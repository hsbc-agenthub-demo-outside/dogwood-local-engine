# dogwood-http：把 dogwood 的两个 socket 翻成 HTTP + Swagger

一个**部署适配器**，不是引擎。它不链接引擎、不打开 store、不碰策略——手里只有两个
socket 路径和一个超时，每个请求就是一次「4 字节长度前缀 + JSON」往返。它能对引擎做的
事，任何语言写 20 行 socket 客户端都能做。

```text
agent / 你的 harness
  └─ HTTP + JSON
       ▼
  dogwood-http（本 crate）  ──Unix socket──►  dogwood-server  ──►  策略 + 历史
       ▲
   Swagger UI（/docs）
```

## 两个面就是两个服务端口，不能合并

|  | 数据面 | 控制面 |
|---|---|---|
| 默认监听 | `127.0.0.1:8080` | `127.0.0.1:8081` |
| 路由 | `POST /v1/events`、`GET /v1/ping`、`GET /healthz` | `/v1/policy*`、`/v1/status`、`/v1/schema`、`/v1/checkpoint`、`/healthz` |
| 鉴权 | 默认无（靠 loopback 绑定） | **必须** bearer token |
| OpenAPI | `/openapi.json` + `/docs` | `/openapi.json` + `/docs` |

两份 OpenAPI 文档**互不提及对方的任何路由**——所以从数据面文档生成的客户端，
在类型上就表达不出「改策略」这件事。这正是引擎两个 socket 的动词隔离，往上抬了一层。

`--plane data` 与 `--plane control` 分进程跑，才恢复引擎原本的分离；
`--plane both` 是单进程图省事，它会在启动时说清楚这一点。

## 状态码契约（这才是真正的接口）

| 情况 | 状态码 | 调用方该做什么 |
|---|---|---|
| allowed | 200 | 放行 |
| **denied** | **403** | 拒绝 |
| recorded（历史事件） | 200 | 放行（没有要执行的动作） |
| 请求体有问题 / 未知字段 | 400 | 拒绝 |
| 超过引擎 16 MiB 帧上限 | 413 | 拒绝 |
| bearer token 缺失或错误 | 401 | 拒绝 |
| 引擎回了 error | 502 | 拒绝 |
| 引擎不可达 / 传输失败 | 502 | 拒绝 |
| 引擎在超时内没回答 | 504 | 拒绝 |

**deny 是 403，不是「200 + `allowed:false`」。** 这是刻意反直觉的：这样一来，
「看 2xx 就放行」和「没抛异常就放行」两种天真客户端**都是 fail-closed**，
唯一会出错的写法是专门把 403 解读成许可。代价是 deny 会进 4xx 指标，看板要把它排除。
引擎自己的 CLI 先做了同样的选择：`dogwood-server submit` 遇到 deny 时退出码非零，
这样 shell 里的 `&&` 才是「允许才继续」。

另外：**504 是歧义的**，引擎可能已经 append 了也可能没有，而引擎不做去重，
所以不要盲目重试——拒绝这次动作，事后再对齐。

## 本地跑（不用 Docker）

数据面在哪都能跑；控制面**只在 Linux 上可用**（见下）。

```bash
cargo build --release -p dogwood-server -p dogwood-http

# 1) 引擎
./target/release/dogwood-server --dir /tmp/dw run

# 2) 适配器
DOGWOOD_CONTROL_TOKEN=dev-only-token \
  ./target/release/dogwood-http serve --store-dir /tmp/dw

# 3) 打开
open http://127.0.0.1:8080/docs
open http://127.0.0.1:8081/docs   # 点 Authorize 填 token
```

### 在 macOS 上，控制面一定会失败——而且这是对的

引擎的控制面用内核 peer credential（`SO_PEERCRED`）鉴权，这是 Linux 专有；
非 Linux 上 `peer_cred()` 直接报错，fail-closed 拒绝**所有**控制连接。所以本机
`/v1/policy` 会失败，数据面则正常。要在线装策略，走下面的 Docker——容器是 Linux。

## Docker 跑

```bash
docker build -f Dockerfile.allinone -t dogwood-allinone .
docker run -d --name dogwood --restart unless-stopped \
  -e DOGWOOD_CONTROL_TOKEN="$(openssl rand -hex 32)" \
  -p 127.0.0.1:8080:8080 -p 127.0.0.1:8081:8081 \
  -v dogwood-state:/var/lib/dogwood \
  dogwood-allinone

./dogwood-http/examples/smoke.sh      # 10 条断言，含真引擎的 403 → 200
open http://127.0.0.1:8080/docs
```

一个容器、一个 uid、四件事：引擎、数据面适配器、控制面适配器、以及一个自己探自己的
健康检查。完整说明见 [`../docs/单容器部署（all-in-one）.md`](<../docs/单容器部署（all-in-one）.md>)。

**这个拓扑没有数据面/控制面的 uid 隔离**：两个面在同一个进程里，所以"接收 agent 事件的
进程"也能改策略。它适合单机 / 单租户 / 可信 agent。要那条边界，得把两个面拆成不同 uid 的
容器——而拆开之后会踩到 §"部署时踩到的两件事"里的两个坑（现在这个部署一个都不受影响，
因为同容器同 uid 天然满足那两个前提）。

## 拆成多容器时会踩到的两件事（单容器部署不受影响）

### 1. 低权限 uid 连不上数据 socket——上游布局做不到它注释里宣称的事

`Paths` 的注释说 `<dir>/run/` 是 `0711`：「agent 可以连它被告知的那个 socket，
但不能列目录」。但 `<dir>` 本身被 `Server::open` 设成了 `0700`，而**路径解析要求
沿途每个目录都有 x 权限**，所以另一个 uid 在 `<dir>` 上就被挡住，`run/` 的 `0711`
根本轮不到生效：

```text
uid 10002 → connect /var/lib/dogwood/run/dogwood.sock
          → Permission denied (os error 13)     # 因为 /var/lib/dogwood 是 0700
```

也就是说：**任何非引擎 uid 的 agent 都连不上数据 socket**，而「低权限 agent」
恰恰是这套设计的前提。compose 里的绕法是让 socket 目录单独成卷，只把它挂给数据面容器
（`dogwood-run:/run/dogwood`），于是路径上不再有那个 0700 目录——顺带把 store 和
control socket 从数据面容器里彻底移除，比裸机布局还严。

上游若要在裸机上支持跨 uid，得让 `<dir>` 可被遍历（`0711`），同时保证 store 文件不可读
（以 `0600` 创建）。目前两者都没有。

### 2. 跨 PID namespace 时，控制面的 peer 认证会失败

在 Docker Desktop 上，控制面适配器如果在**自己的** PID namespace 里连引擎：

```text
peer attestation failed: SO_PEERCRED: Unknown error -10001 (os error -10001)
```

同容器内连接则正常。逐层排下来：Python 用 libc 读同一场景（跨容器连）能拿到
`pid=0 uid=10001`，说明内核侧没问题；让客户端**共享引擎的 PID namespace** 之后
`SO_PEERCRED` 立刻成功。所以是 peer 的 pid 跨 namespace 时不可映射（内核报 0），
而 `dogwood-server` 的 `peer_cred` 在这个情况下报了一个畸形的错误码。

compose 的绕法是 `pid: "service:engine"`。这也是为什么控制面适配器必须和引擎同一个
uid：control socket 是 `0700`，别的 uid 在 `connect()` 就被拒——引擎的 `--control-uid`
旗标看着能放宽，但 0700 的 socket 其实轮不到白名单生效。

## 这个 HTTP 面换掉了什么

**引擎的鉴权没了，换成一个 bearer token。** 引擎控制面靠内核认证的 uid：客户端不提供
它、也伪造不了，「谁能改策略」=「你是哪个 OS 用户」——OS 已经拥有这个身份，也拥有吊销
手段。TCP 上没有等价物，所以 `src/auth.rs` 实现了能替代它的最小东西。它严格更弱：
token 会从日志和 shell history 里泄漏、自己不会过期、认证的是「一个部署」而不是「一个人」。

两个操作上的后果，都在启动时说出来而不是只写在注释里：

1. 控制面默认绑 loopback。绑到别处会打警告（用 `--allow-non-loopback-control`
   承认这个决定后降级成提示）——那是唯一一个把「本地工具」变成「网络上谁都能改策略的
   端点」的配置改动。
2. 4xx/5xx 一律等于「没有裁决」。引擎的其他保证都还在（先落盘后裁决、
   store 赋的不可伪造时间戳、评估出错时 fail-closed、install 被拒时旧策略继续服务），
   因为它们在 socket 的另一侧，这个适配器改不动它们。

## 容易踩的坑

- **动作名在 wire 上不带引号。** 策略里写 `AgentCore::Action::"refund"`，
  事件里发 `AgentCore::Action::refund`。写错的表现和「策略写错了」一模一样：全部 DENY。
- **`logged` 和 `context` 是两个包。** 一个字段两边都要读，就两边都发。
- **每条事件都要带 `principal`。** 历史按 principal 分键（demo 的 `partition_key`
  就是 `callerPrincipal`），漏了它历史会落到别的键上，症状是静默 DENY。
- **`response` 事件不是可选的。** `formerly` 匹配的是历史里的 response，
  少了它窗口永远是空的。
