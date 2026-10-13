# dogwood-sdk：Dogwood 的 Python 客户端（本地 socket / 远端 HTTP）

**零依赖**的 Python 客户端（标准库之外什么都不用）。两种传输，一套 API：

| endpoint | 传输 | 什么时候用 | 凭什么认证 |
|---|---|---|---|
| `tcp://host:9000` | **引擎自己的协议走 TCP**（主方案） | 跨机 | 转发器自己的 uid（见下） |
| `/run/dogwood/dogwood.sock`、`unix://…` | 引擎自己的 socket | 同机 | 内核按 uid |
| `http://host:8080`、`https://host` | `dogwood-http` 适配器（备选） | 跨机、要 OpenAPI | bearer token |

```python
DogwoodClient("tcp://dogwood.internal:9000")          # 主方案：引擎协议走 TCP
DogwoodClient("/run/dogwood/dogwood.sock")            # 同机
DogwoodAdminClient("http://dogwood.internal:8081", token="…")   # 备选：HTTP
```

两个传输返回**同样的类型、同样的失败规则**，所以切换只改一行。

⚠️ `tcp://` 的**控制面**端口要收窄：转发器以自己的 uid 连引擎，所以引擎认证的是转发器，
能连到那个端口的人就都能改策略。只发布到 loopback，或者前面放认证反代。

本地那条的整个 wire 格式就是 [`dogwood_sdk/framing.py`](dogwood_sdk/framing.py)；
HTTP 那套的路由映射在 [`dogwood_sdk/transport.py`](dogwood_sdk/transport.py)。

> 这份是**包说明**（它是什么、为什么这样设计、怎么部署和测试）。
> 完整的 **API 用法与例子**在
> [docs/Python SDK 使用手册.md](<../docs/Python SDK 使用手册.md>) —— 那一份里每段输出
> 都是在跑着的服务上实跑出来的。

```python
from dogwood_sdk import DogwoodClient

dogwood = DogwoodClient(store_dir="/tmp/dw")     # agent 那一侧
decision = dogwood.decide(
    "AgentCore::Action::refund", "request",
    principal='AgentCore::User::"alice"',
    context={"input": {"amount": 500}},
)
if not decision.allowed:
    return "refused"
```

## 一条规则

**引擎只出裁决，执行是调用方的事。** 这个包不拦工具、不杀进程、也不验证事件里的
`principal`——那是 harness 的责任。而把"连不上引擎"当成"放行"的客户端，
等于引擎根本没过问就被打败了。

所以这里**每一个失败都抛异常**（见 [`errors.py`](dogwood_sdk/errors.py)），
让异常逃出去就是安全的。不想写 `except` 的调用方用 `allows()`，
它按构造在每种失败下返回 `False`：

```python
if dogwood.allows("AgentCore::Action::refund", "request", **refund):
    run_refund()
```

日志会记下它为什么拒绝——变成 deny 不等于变成沉默。

## 两个客户端，两个 socket，两种权限

| | `DogwoodClient` | `DogwoodAdminClient` |
|---|---|---|
| 面 | 数据面 | 控制面 |
| socket | `<dir>/run/dogwood.sock` | `<dir>/control.sock` |
| 动词 | `submit`、`ping` | `install`、`batch`、`status`、`list_policies`… |
| 谁能用 | 被治理的进程 | 运维 |
| 能不能改策略 | **不能**（socket 上根本没这个动词） | 能 |

这不是约定俗成，是引擎的动词集决定的：`DataRequest` 里没有读/改/停策略的变体。
把 `DogwoodAdminClient` 交给被治理的进程，等于把改写审判自己的规则的权力交出去。

## 装

不需要装。把 `python-sdk/` 放进 `PYTHONPATH` 就行：

```bash
PYTHONPATH=python-sdk python3 -c "from dogwood_sdk import DogwoodClient; print('ok')"
```

想安装成包也可以（`pyproject.toml` 里 `dependencies = []`，装它不拉任何东西）：

```bash
pip install ./python-sdk
```

## 部署

都是一个容器，两种方式——SDK 的 endpoint 决定它连哪个
（见 [单容器部署（all-in-one）](<../docs/单容器部署（all-in-one）.md>)）：

| 部署 | endpoint | 怎么起 |
|---|---|---|
| HTTP all-in-one（跨机） | `http://host:8080` / `:8081` | 下面这段 |
| socket all-in-one（同机） | `/run/dogwood/dogwood.sock` | `docker exec -u dogwood-agent dogwood dogwood-entrypoint demo` |

```bash
docker build -f Dockerfile.allinone -t dogwood-allinone .
docker run -d --name dogwood -p 127.0.0.1:8080:8080 -p 127.0.0.1:8081:8081 \
  --env-file .env -v dogwood-state:/var/lib/dogwood dogwood-allinone

PYTHONPATH=python-sdk python3 examples/install_policy.py   # 见 examples/
PYTHONPATH=python-sdk python3 examples/decide.py           # 四步断言，退出码即结果
```

`examples/decide.py` 断言四步都符合策略（无批准被拒 → 记录批准 → 同一笔放行 →
换金额又被拒）。

## 这个 SDK 刻意不做的事

- **不重试。** 一次 submit 超时是**歧义**的：引擎可能已经 append 了也可能没有，
  而它不做去重。盲目重试可能重复写一条历史事件，改变后续 `formerly` /
  `count_within` 看到的东西。`DogwoodTimeoutError` 是留给调用方解决的，
  不是这个包该糊过去的。
- **不维护长连接。** 每次调用开一条连接、发一帧、读一帧、关掉。比 fsync 便宜得多，
  换来两件事：不用处理引擎 30 秒空闲回收带来的重连，也不可能出现"陈旧的连接变成
  陈旧的裁决"。
- **不提供时间戳参数。** `WireEvent` 里没有这个字段，时间由 store 在 append 时赋予，
  所以客户端无法把事件挪出窗口或插队。赋的值通过 `recorded_at_nanos` 回来。
- **不拦截、不执行、不认证身份。** 那是 harness 的事。

## 测试

```bash
# 纯协议层，不需要引擎
PYTHONPATH=python-sdk python3 -m unittest discover -s python-sdk/tests -p 'test_framing.py' -v

# 对着真引擎
PYTHONPATH=python-sdk python3 -m unittest discover -s python-sdk/tests -p 'test_live.py' -v
```

`test_live.py` 有两种取引擎的方式：默认自己起一个（找 `target/release` →
`target/debug`，或 `DOGWOOD_SERVER_BIN`）；或者用 `DOGWOOD_TEST_STORE_DIR`
指向一个**已经在跑**的同机引擎。

对 HTTP 部署的那部分是 `test_http_transport.py`，给上 endpoint 就会跑：

```bash
DOGWOOD_ENDPOINT=http://127.0.0.1:8080 \
DOGWOOD_CONTROL_ENDPOINT=http://127.0.0.1:8081 \
DOGWOOD_CONTROL_TOKEN=$(sed -n 's/^DOGWOOD_CONTROL_TOKEN=//p' ../.env) \
python3 -m unittest discover -s tests
```

16 个测试全绿（在 Linux 容器里）。注意平台差异是被**断言**而不是跳过的：
macOS 上控制面必然失败，测试断言的就是"它会拒绝"；Linux 上则断言它正常工作。

## 和 `python-demo/dogwood_client.py` 的关系

那个是 demo 的一部分：单文件、跟 `python-demo/` 的中间件和场景绑在一起。
这个包是独立的 SDK——类型化返回值、完整的控制面动词、异常分类、
针对真引擎的测试。两者说同一个协议（都以 `protocol.rs` 为准），
demo 那份可以继续照常用；如果你要在自己的 agent 里集成，用这个包。
