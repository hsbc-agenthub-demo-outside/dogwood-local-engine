# Python SDK 上手

五分钟跑通：**装策略 → 问裁决 → 记结果**。

SDK 说引擎**自己的协议**（4 字节大端长度 + JSON），通过 TCP 连过去；中间只有一个纯字节
搬运的转发器，没有第二套 API。零依赖。

## 1. 先有服务

```bash
cd /Users/cengshilin/study/dogwood-local-engine

docker build -f Dockerfile.socket-allinone -t dogwood-socket-allinone .
docker run -d --name dogwood --restart unless-stopped \
  -e DOGWOOD_TCP_DATA_BIND=0.0.0.0:9000 \
  -e DOGWOOD_TCP_CONTROL_BIND=0.0.0.0:9001 \
  -p 127.0.0.1:9000:9000 -p 127.0.0.1:9001:9001 \
  -v dogwood-state:/var/lib/dogwood \
  -v dogwood-run:/var/lib/dogwood/run \
  dogwood-socket-allinone
```

两个地址，记住这两个就够：

| | 地址 | 干什么 |
|---|---|---|
| 数据面 | `tcp://127.0.0.1:9000` | 上报事件、问裁决 |
| 控制面 | `tcp://127.0.0.1:9001` | 装/改策略 |

> **控制面那个端口要小心**：转发器是以引擎的 uid 连 socket 的，所以**能连到这个端口的
> 人就能改策略**。上面的 `-p` 只发布到 loopback；要暴露到别处，前面得放认证反代，或者
> 改用 `DOGWOOD_POLICY_FILE` 在启动时装策略、干脆不开控制面端口。
>
> 想走 HTTP（备选方案）见 [HTTP 对接指南](<HTTP 对接指南.md>)——endpoint 换成 `http://…`
> 即可，其余代码不用改。

## 2. 用起来

不用装，加个 `PYTHONPATH` 就行（`pip install ./python-sdk` 也可以）：

```bash
PYTHONPATH=python-sdk python3 hello.py
```

## 3. Hello World

```python
import time
from dogwood_sdk import DogwoodClient, DogwoodAdminClient

CONTROL = "tcp://127.0.0.1:9001"         # 运维侧
DATA    = "tcp://127.0.0.1:9000"         # agent 侧

# 1) 装一套策略（演示用的：退款必须有一小时内的同额批准）
admin = DogwoodAdminClient(CONTROL)
admin.install_files("python-demo/demo_policy.dw",
                    "python-demo/demo_schema.cedarschema")
print("策略已装:", admin.status().rule_count, "条规则")

# 2) 问裁决
dogwood = DogwoodClient(DATA)
amount = time.time_ns() % 1_000_000 + 1000   # 每次换个金额，保证下一步是 DENY
call = dict(principal='AgentCore::User::"alice"',
            resource='AgentCore::Account::"main"',
            logged={"input": {"amount": amount}},
            context={"input": {"amount": amount}})

print("退款(还没批准):", dogwood.decide("AgentCore::Action::refund", "request", **call))

# 3) 批准工具跑完了，把结果记进历史（时序规则靠它）
dogwood.record("AgentCore::Action::approve", "response",
               principal=call["principal"], resource=call["resource"],
               logged={"input": {"amount": amount}, "output": {"approved": True}},
               context={"input": {"amount": amount}})

# 4) 再问一次
print("退款(已批准):  ", dogwood.decide("AgentCore::Action::refund", "request", **call))
```

真实输出：

```console
策略已装: 2 条规则
退款(还没批准): DENY at=1791868787040414542ns
退款(已批准):   ALLOW at=1791868787043274626ns
```

（`python-demo/` 那两个是仓库自带的示例策略，路径相对仓库根目录；换成你自己的
`.dw` 和 `.cedarschema` 就行。）

## 4. SDK 就这些方法

**`DogwoodAdminClient(endpoint)`** —— 运维侧

| 方法 | 作用 |
|---|---|
| `install_files(policy, schema)` | 装一整套策略（替换现有的） |
| `install(policy, schema)` | 同上，直接传字符串 |
| `status()` | `rule_count` / `decision_kinds` / `log_offset` |
| `add(policy)` / `update(id, …)` / `delete(id)` | 增量改单条，保留历史 |

**`DogwoodClient(endpoint)`** —— agent 侧

| 方法 | 作用 |
|---|---|
| `decide(action, kind, **event)` | 问裁决，返回 `Decision` |
| `record(action, kind, **event)` | 记历史事件，返回 `Recorded` |
| `allows(action, kind, **event)` | 上面那个的省心版，**任何失败都返回 `False`** |
| `ping()` | 探活 |

`Decision` 只有四个字段：`allowed`、`reason`（决定它的策略 id）、`errors`、
`recorded_at_nanos`。

事件字段：`principal`（谁）、`resource`（对什么）、`logged`（写进历史）、
`context`（只给这次决策读）。**`action` 在 wire 上不带引号**——策略里写
`AgentCore::Action::"refund"`，这里发 `AgentCore::Action::refund`。

## 5. 三条会踩的规则

1. **拒绝不是异常。** `decide()` 遇到拒绝会正常返回 `Decision(allowed=False)`；
   只有传输失败和超时才抛异常。两种情况对调用方的含义不同：前者是"引擎说不行"，
   后者是"没拿到裁决"。
2. **任何异常都等于"不许做"。** 用 `allows()` 就不用自己写 `except`——
   引擎不可达、超时、协议错，它全返回 `False`。
3. **`record` 不是可选的。** 时序规则（`formerly` / `count_within`）匹配的是历史里的
   `response` 事件。只在请求时问、做完不记，所有时序规则都会表现成"策略写错了"。

## 6. 跑不通先看这里

| 症状 | 原因 |
|---|---|
| 全是拒绝 | 策略没装（先看 `admin.status().rule_count`），或动作名带了引号，或事件没带 `principal` |
| 批准过还是被拒 | 没记 `response` 事件，或金额/principal 对不上，或超出时间窗口 |
| `connect tcp://…: Connection refused` | 容器没起、端口没发布（`-p`），或 `DOGWOOD_TCP_*_BIND` 没设 |
| `DogwoodEngineError: … unknown field 'ts'` | 事件里带了时间戳——引擎不接受，时间由 store 在 append 时赋 |
| `no policy set installed` | 还没 `install` |
| 控制面能连上但你没打算公开它 | 读 [部署文档](<单容器部署（all-in-one）.md>) 里那条警告 |

## 7. 更多

- 部署（socket + TCP 主方案、HTTP 备选）：[单容器部署（all-in-one）](<单容器部署（all-in-one）.md>)
- HTTP 契约（状态码、两个面）：[HTTP 对接指南](<HTTP 对接指南.md>)
- 测试矩阵与排障：[调试与测试](调试与测试.md)
