# HTTP 对接指南（备选方案）

HTTP 是把引擎的 socket 面翻成普通 HTTP 的那一层。**这是备选方案**——主方案是
[socket + TCP](<单容器部署（all-in-one）.md>)，SDK 直接说引擎自己的协议。
留这份是为了对比：HTTP 面多一层适配器、多一份状态码契约、鉴权方式也不同。

用 HTTP 时你只会碰到**两个 URL + 一个 token**，不需要知道任何 socket 路径。

## 0. 先把一件事说清楚

| 你的说法 | 能不能 | 说明 |
|---|---|---|
| 我的应用用 HTTP 对接 | ✅ **完全支持** | 本文全部内容，`http://host:port` |
| 我只想要"TCP" | ⚠️ 有两种 | **引擎自己的协议走 TCP**（主方案，见部署文档）或 **HTTP**（本文）。前者少一层、少一份契约；后者任何语言都现成能接 |
| 我的部署里不希望出现 socket 文件 | ❌ 做不到 | 引擎**只会**说 Unix socket，适配器（或 TCP 转发器）是把它翻出来的那一层。socket 仍在**容器内部**，只是在你不需要关心的那一段 |

所以"不考虑 `control.sock`"在你的集成层是成立的：**你从头到尾都不需要碰它。**

## 1. 部署

两种，都 TCP 对外，都可用：

```bash
cd /Users/cengshilin/study/dogwood-local-engine
cp .env.example .env
echo "DOGWOOD_CONTROL_TOKEN=$(openssl rand -hex 32)" > .env

docker build -f Dockerfile.allinone -t dogwood-allinone .
docker run -d --name dogwood --restart unless-stopped \
  --env-file .env \
  -p 127.0.0.1:8080:8080 -p 127.0.0.1:8081:8081 \
  -v dogwood-state:/var/lib/dogwood \
  dogwood-allinone
```

两个端口：

| 端口 | 是什么 | 鉴权 |
|---|---|---|
| `:8080` | 数据面：上报事件、拿裁决 | 默认无（见 §4） |
| `:8081` | 控制面：装/改策略、看状态 | **必须** bearer token |

**`-p` 请写成 `127.0.0.1:...`**。控制面只靠 token 保护；要暴露到别的网卡，
前面必须有真正做认证的反代。

## 2. 你的代码（这是全部）

```python
import os
from dogwood_sdk import DogwoodClient, DogwoodAdminClient

DATA = os.environ.get("DOGWOOD_URL", "http://127.0.0.1:8080")
CONTROL = os.environ.get("DOGWOOD_CONTROL_URL", "http://127.0.0.1:8081")

# 运维侧：装策略（token 也可以从 DOGWOOD_CONTROL_TOKEN 读）
admin = DogwoodAdminClient(CONTROL, token=os.environ["DOGWOOD_CONTROL_TOKEN"])
admin.install_files("policy.dw", "schema.cedarschema")

# agent 侧：问裁决 + 记结果
dogwood = DogwoodClient(DATA)

call = dict(principal='AgentCore::User::"alice"',
            resource='AgentCore::Account::"main"',
            logged={"input": {"amount": 1500}},
            context={"input": {"amount": 1500}})

if not dogwood.allows("AgentCore::Action::refund", "request", **call):
    return "refused"                     # 任何失败都是 False

result = run_the_tool()

# 这一步不能省：时序规则匹配的是历史里的 response 事件
dogwood.record("AgentCore::Action::refund", "response",
               principal=call["principal"], resource=call["resource"],
               logged={"input": {"amount": 1500}, "output": result},
               context={"input": {"amount": 1500}})
```

实测（SDK 在 macOS 上，Dogwood 在 Docker 里，全程只有 URL）：

```console
装策略 -> 2 rules
退款(无批准) -> DENY at=1791867125467253010ns
退款(有批准) -> ALLOW at=1791867125470048343ns
allows      -> True
```

### 不用 SDK 也行

就是普通 HTTP，任何语言都能接：

```bash
# 装策略
curl -s -X POST -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
     --data-binary @install.json http://127.0.0.1:8081/v1/policy

# 问裁决
curl -s -i -X POST -H 'Content-Type: application/json' \
     --data-binary '{"action":"AgentCore::Action::refund","kind":"request",
                     "principal":"AgentCore::User::\"alice\"",
                     "context":{"input":{"amount":1500}}}' \
     http://127.0.0.1:8080/v1/events
```

## 3. 状态码就是契约

| 状态 | 含义 | 你该做什么 |
|---|---|---|
| 200 | allow（或历史事件已入账） | 放行 |
| **403** | **deny** | **拒绝**——注意它不是错误，是裁决 |
| 400 | 请求体有问题（未知字段会报出字段名） | 拒绝 |
| 401 | token 缺失或错误 | 检查 token |
| 502 | 适配器活着，但它连不上引擎 | 拒绝，查适配器日志 |
| 504 | 引擎在超时内没答 | 拒绝；**别盲目重试 submit**（引擎不去重） |

**403 而不是 `200 + allowed:false`** 是刻意的：这样"看 2xx 就放行"和"没抛异常就放行"
两种天真写法都自动 fail-closed。代价是 deny 会进 4xx 指标，看板要排除。

SDK 已经把这些翻译好了：403 → `Decision(allowed=False)`（不抛异常）；401 →
`DogwoodAuthError`；502 → `DogwoodTransportError`；504 → `DogwoodTimeoutError`。

## 4. 数据面要不要 token（TCP 部署必须决定）

**默认没有。** 数据面的保护是"只绑 loopback"——引擎自己的数据 socket 也是这么设计的
（任何人只要能穿过目录就能连）。一旦你把 8080 暴露到别的网卡，就等于**谁都能上报事件、
拿裁决**。

在你的 `.env` 里加一行，重建数据面即可：

```bash
echo "DOGWOOD_DATA_TOKEN=$(openssl rand -hex 32)" >> .env
docker rm -f dogwood && docker run -d … --env-file .env …      # 同样的 docker run，重来一次
```

验证（我用一个临时容器实测过）：

```console
不带 token -> 401
带对 token -> 200
```

SDK 会自动读 `DOGWOOD_DATA_TOKEN`，也可以显式传：

```python
DogwoodClient("http://dogwood.internal:8080", token=os.environ["DOGWOOD_DATA_TOKEN"])
```

判断标准很简单：**8080 只在 loopback 上 → 可以不开；要跨机 → 必须开**（或前面放反代）。

## 5. 验证对接是否成功

```bash
# 活着吗（数据面探针会去 ping 引擎，所以 200 = 引擎也在答）
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8080/healthz

# 控制面 + token
curl -s -H "Authorization: Bearer $TOKEN" http://127.0.0.1:8081/v1/status
```

浏览器：`http://127.0.0.1:8080/docs`（数据面）、`http://127.0.0.1:8081/docs`
（控制面，点 Authorize 粘 token，**不要**加 `Bearer ` 前缀）。

一键全链路自检（不需要 SDK、不需要 socket）：

```bash
./dogwood-http/examples/smoke.sh
python3 dogwood-http/examples/testcases.py --all
```

## 6. 常见错误

| 症状 | 原因 |
|---|---|
| 所有请求 403 | 没装策略（先看 `GET /v1/status` 的 `rule_count`），或动作名带了引号（wire 上是 `AgentCore::Action::refund`，**不带引号**） |
| 批准过还是被拒 | 少了 `response` 事件，或金额/principal 对不上，或超出时间窗口 |
| 401 | token 不在、拼错、或粘的时候多打了 `Bearer ` 前缀 |
| 改了 `.env` 但 token 不生效 | `restart` 不重读环境变量；用 `up -d`（会重建容器） |
| 502 | 适配器连不上引擎；看 `docker logs dogwood` |
| `unknown field 'ts'` | 事件里带了时间戳——引擎不接受，时间由 store 在 append 时赋 |
| 从别的机器连不上 | compose 里发布的是 `127.0.0.1:`；这是刻意的，见 §1 |

## 7. 相关文档

| 文档 | 内容 |
|---|---|
| [调试与测试](调试与测试.md) | 测试矩阵、排障、踩过的坑 |
| [Python SDK 使用手册](<Python SDK 使用手册.md>) | SDK 全部 API 与例子（两种 endpoint） |
| [../dogwood-http/README.md](../dogwood-http/README.md) | 适配器本身：状态码契约、两个面、安全取舍 |
