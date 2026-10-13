# 单容器部署（all-in-one）

**一个容器、一条 `docker run`**，两种对外方式：

| | **socket + TCP**（主方案） | **HTTP**（备选，方便对比） |
|---|---|---|
| 镜像 | `dogwood-socket-allinone` | `dogwood-allinone` |
| 构建 | `Dockerfile.socket-allinone` | `Dockerfile.allinone` |
| 对外 | 引擎**原样的协议**走 TCP（默认 `:9000` / `:9001`），外加两个 Unix socket | HTTP `:8080` / `:8081` |
| 客户端 | `dogwood_sdk` 直接说引擎协议（`tcp://host:port`） | 任何 HTTP 客户端；SDK 也能（`http://…`） |
| 鉴权 | 内核 uid（+ 转发端口自己要收窄） | 控制面 bearer token |
| 中间层 | 一个**纯字节搬运**的转发器（不解析协议） | 适配器（解析、映射、出 OpenAPI） |

**主方案为什么是它**：dogwood 本来就是 socket 协议，`socket + TCP` 只是让这个协议能被
另一个机器访问，SDK 说的还是引擎自己的话——没有第二套 API、没有状态码契约要记。
HTTP 那套多一层适配器、多一份契约，留在这里是为了对比和"任何语言都能接"的场合。

---

## 一、socket + TCP（主方案）

### 起

```bash
cd /Users/cengshilin/study/dogwood-local-engine

docker build -f Dockerfile.socket-allinone -t dogwood-socket-allinone .

docker run -d --name dogwood --restart unless-stopped \
  -e DOGWOOD_TCP_DATA_BIND=0.0.0.0:9000 \
  -e DOGWOOD_TCP_CONTROL_BIND=0.0.0.0:9001 \
  -p 127.0.0.1:9000:9000 -p 127.0.0.1:9001:9001 \
  -v dogwood-state:/var/lib/dogwood \
  -v dogwood-run:/var/lib/dogwood/run \
  -v dogwood-run:/run/dogwood \
  dogwood-socket-allinone
```

`-v dogwood-run:/run/dogwood` 是给容器内的 `docker exec` 用的；只用 TCP 的话可以省掉。

### 启动日志

```
dogwood-entrypoint: data socket    (agent)   /run/dogwood/dogwood.sock
dogwood-entrypoint: control socket (operator) /var/lib/dogwood/control.sock
bridge: data: tcp 0.0.0.0:9000 -> /var/lib/dogwood/run/dogwood.sock（uid 10001）
bridge: control: tcp 0.0.0.0:9001 -> /var/lib/dogwood/control.sock（uid 10001）
```

### 用（SDK，说引擎自己的协议）

```python
import time
from dogwood_sdk import DogwoodClient, DogwoodAdminClient

admin = DogwoodAdminClient("tcp://127.0.0.1:9001")     # 控制面
admin.install_files("python-demo/demo_policy.dw", "python-demo/demo_schema.cedarschema")

dogwood = DogwoodClient("tcp://127.0.0.1:9000")        # 数据面
amount = time.time_ns() % 1_000_000 + 1000
call = dict(principal='AgentCore::User::"alice"',
            resource='AgentCore::Account::"main"',
            logged={"input": {"amount": amount}},
            context={"input": {"amount": amount}})

print(dogwood.decide("AgentCore::Action::refund", "request", **call))   # DENY

dogwood.record("AgentCore::Action::approve", "response",
               principal=call["principal"], resource=call["resource"],
               logged={"input": {"amount": amount}, "output": {"approved": True}},
               context={"input": {"amount": amount}})

print(dogwood.decide("AgentCore::Action::refund", "request", **call))   # ALLOW
```

实测（SDK 在 macOS 上，引擎在容器里，中间是 TCP）：

```console
装策略 -> 2 rules, decision kinds ['request']
ping   -> 0.1.0
退款(没批准) -> DENY at=1791868787040414542ns
退款(已批准) -> ALLOW at=1791868787043274626ns
allows      -> True
```

### 那条警告必须看

转发器是**以自己的 uid** 连引擎的，所以引擎认证到的是转发器，不是 TCP 那头的人。于是：

* **数据面端口（9000）**：影响不大——数据 socket 本来就是 `0666`，谁都能连；
* **控制面端口（9001）**：**能连到这个端口的人就能改策略**。控制 socket 是 `0700` +
  内核 uid 白名单，而转发器以引擎的 uid 跑（否则它连不上），这道门就绕过了。

所以：**9001 要么只发布到 loopback（`-p 127.0.0.1:9001:9001`，上面就是这么写的），
要么前面放一个真做认证的反代。** 引擎容器里必须绑 `0.0.0.0`——Docker 发布的端口只会
投递到容器的网卡地址，不会到它的 loopback，**收窄要写在 `-p` 上**。

如果不想开控制面端口：用 `DOGWOOD_POLICY_FILE` 在启动时装策略，只暴露 9000。

### 只要 socket、不要 TCP

两个 `-e` 都不给就是纯 socket（没有端口，靠 `docker exec`）：

```bash
docker run -d --name dogwood -v dogwood-state:/var/lib/dogwood \
  -v dogwood-run:/var/lib/dogwood/run -v dogwood-run:/run/dogwood \
  dogwood-socket-allinone

docker exec dogwood dogwood-entrypoint install \
  /opt/dogwood/examples/demo_policy.dw /opt/dogwood/examples/demo_schema.cedarschema
docker exec -u dogwood-agent dogwood dogwood-entrypoint demo
docker exec dogwood dogwood-entrypoint test
```

> `dogwood-run` 挂了两次不是笔误：状态目录是 `0700`，它下面的 socket 对别的 uid
> 一律不可达（socket 自己是 `0666` 也没用，路径解析在上一级就 `EACCES`），所以
> socket 目录要单独成卷、挂在一条可穿越的路径上。忘了挂第二个，入口会指出来。

---

## 二、HTTP（备选）

```bash
docker build -f Dockerfile.allinone -t dogwood-allinone .
docker run -d --name dogwood --restart unless-stopped \
  -e DOGWOOD_CONTROL_TOKEN="$(openssl rand -hex 32)" \
  -p 127.0.0.1:8080:8080 -p 127.0.0.1:8081:8081 \
  -v dogwood-state:/var/lib/dogwood \
  dogwood-allinone

./dogwood-http/examples/smoke.sh      # 10 条断言
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8080/healthz
```

`:8080` 数据面、`:8081` 控制面（要 token）。Swagger 在 `/docs`。
**deny 是 HTTP 403，不是 `200 + allowed:false`**——这样「看 2xx 就放行」的客户端天然
fail-closed。完整契约见 [HTTP 对接指南](<HTTP 对接指南.md>)。

---

## 三、环境变量

| 变量 | 默认 | 作用 |
|---|---|---|
| `DOGWOOD_STORE_DIR` | `/var/lib/dogwood` | 引擎状态目录 |
| `DOGWOOD_TCP_DATA_BIND` | — | 数据 socket 的 TCP 监听（如 `0.0.0.0:9000`） |
| `DOGWOOD_TCP_CONTROL_BIND` | — | 控制 socket 的 TCP 监听（**读完上面那条警告**） |
| `DOGWOOD_POLICY_FILE` + `DOGWOOD_ACTION_SCHEMA_FILE` | — | 启动时装策略（每次启动都**替换**整个策略集；用它就不必开控制面端口） |
| `DOGWOOD_CONTROL_TOKEN` | — | 仅 HTTP 版需要：控制面的 bearer token |
| `DOGWOOD_SNAPSHOT_INTERVAL` | `10000` | 每多少个事件快照一次，`0` 关 |

---

## 四、两个共同的注意点

1. **持久化在卷里，不在容器里。** 删掉容器、用同样的 `docker run` 起来，`rule_count`
   和 `log_offset` 会原样回来。
2. **一个 store 卷同时只能被一个容器打开**（redb 持排他锁）。要并行跑多个引擎，
   用不同的卷。

排障、测试矩阵，见 [调试与测试](调试与测试.md)。
