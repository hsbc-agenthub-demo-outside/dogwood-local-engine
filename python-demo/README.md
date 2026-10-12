# DeepAgents + Dogwood 拦截器 demo

一句话：**用 LangChain 的 `wrap_tool_call` 钩子，在每次工具调用前后跟 Dogwood 说
一句话 —— 调用前问"能不能做"，调用后把结果记进历史。**

这个目录不属于 Cargo workspace（`cargo build --workspace` 不会碰它）。它是一份
可直接运行的 Python demo，演示怎么把 `dogwood-server` 接进 DeepAgents。

## 装依赖

Python 需要 **>= 3.11**（deepagents 自己的要求）。两份已验证的版本组合在
[`requirements.txt`](requirements.txt) 里，锁的是这台机器上跑通的版本：
deepagents 0.6.12 / langchain 1.3.14 / langgraph 1.2.9 / langchain-core 1.5.1。

```bash
# 用你那个 conda 环境（Python 3.12，符合要求）
conda activate dogwood
python -m pip install -r requirements.txt

# 或者完全隔离的 venv
python3 -m venv .venv && source .venv/bin/activate
python -m pip install -r requirements.txt
```

`deepagents` 会连带拉进 langchain / langgraph 和 anthropic、google-genai 两个
provider SDK，一共约 60 个包 —— 这是它默认支持多家模型的结果，正常现象。只用
脚本化假模型的话，**不需要** 任何 API key。

（`conda activate` 报 `Run 'conda init'` 就先 `conda init zsh` 再新开终端。你的
`dogwood` 环境目前是干净的，上面那条 `pip install` 是必须的。）

## 直接跑

```bash
cd python-demo
python doctor.py --stub --deep --probe   # 先体检：环境 + 链路 + 一次真实往返
python test_middleware.py                # 拦截器断言，不需要 LLM / API key
python run_demo.py                       # 完整剧情：DeepAgents 真的跑一遍
```

剧情是"退款必须先被批准"：

```text
[3.1] 退款 500（没有批准历史）  -> ToolMessage: 被策略拒绝，工具没有执行
[3.2] 审批 500                  -> ToolMessage: {"approved": true, ...}，并记入历史
[3.3] 再退款 500（有历史了）    -> ToolMessage: {"refunded": true, ...}，工具真的执行
[4]   退款 999                  -> DENY（金额对不上，历史里那条批准不适用）
```

`run_demo.py` 用一个脚本化的假模型按固定剧本产出工具调用，所以**不需要 API key**、
结果确定可复现。换成真模型只需要替换 `model=`，拦截逻辑一个字都不用改。

## 换成真的 dogwood-server

默认走的是进程内的**离线替身**（`dogwood_stub_server.py`），好让没有 Rust 工具链
的机器也能把链路跑通。替身只复刻了 demo 用到的那一小块协议，**不是** Dogwood
实现：它不解析 `.dw`、不做类型检查、没有持久化 —— 它连一段"根本不是策略"的垃圾
都会被"安装成功"。

### 在 Linux 上（真引擎的正常用法）

```bash
# 1) 装工具链（如果还没有）并构建服务端；第一次会拉 dogwood-language 的 git 依赖
curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh
cargo build --release -p dogwood-server

# 2) 起服务 + 装策略（先校验再原子替换）
./target/release/dogwood-server --dir /tmp/dogwood-demo run &
./target/release/dogwood-server --dir /tmp/dogwood-demo policy install \
    python-demo/demo_policy.dw --action-schema python-demo/demo_schema.cedarschema

# 3) 打真服务
cd python-demo && python3 run_demo.py --no-stub
```

### 在 macOS 上：控制面按设计不可用

`dogwood-server` 的对端认证只实现了 Linux 的 `SO_PEERCRED`；在非 Linux 上
`peer_cred()` **一律返回错误**（fail-closed：宁可服务不可用，也不在没有边界的
平台上"能用"）。所以 macOS 上：

* **控制面全废** —— `policy install` / `status` 都只会回
  `peer attestation failed: peer-credential attestation is only implemented for Linux`；
* **数据面正常** —— 它不需要对端认证。

于是有两条路。要么在 Linux（容器/虚拟机）里跑整套；要么**绕过控制面装策略**：
用一个**独立**的 Rust 小程序（不放进这个 workspace，避免和根 Cargo.toml 冲突）
直接调引擎库，把策略写进 `store`，服务端启动时会恢复它：

```toml
# /tmp/dw-helper/Cargo.toml
[package]
name = "dw-helper"
version = "0.1.0"
edition = "2021"

[dependencies]
dogwood-local-engine = { path = "/path/to/dogwood-local-engine/dogwood-local-engine" }
```

```rust
// /tmp/dw-helper/src/main.rs —— 真实代码见本仓库的提交历史/demo 说明
use dogwood_local_engine::DurableTemporalEngine;

fn main() {
    let dir = std::env::args().nth(1).unwrap();
    let policy = std::fs::read_to_string(std::env::args().nth(2).unwrap()).unwrap();
    let schema = std::fs::read_to_string(std::env::args().nth(3).unwrap()).unwrap();
    let mut engine = DurableTemporalEngine::open(format!("{dir}/dogwood.redb"), 1000).unwrap();
    let applied = engine.install(&policy, &schema, None, None).unwrap();
    println!("rule_count={} leaf_count={}", applied.rule_count, applied.leaf_count);
}
```

装完起服务、跑 demo（`--no-install` 表示"策略已经在 store 里了，别再走控制面"）：

```bash
cd python-demo && ./test_with_real_engine.sh
```

[`test_with_real_engine.sh`](test_with_real_engine.sh) 把这套流程自动化了：必要时
构建服务端 → 在临时目录里生成上面那个小工具、把策略装进**全新**的 store（所以
三幕剧情是干净的）→ 起服务 → 跑 demo → **杀掉服务再起来**验证历史恢复 → 收尾。
全程不需要手改任何文件，store 留在临时目录里可以直接删。

这条路径已经在 macOS 上跑通并验证过：三幕剧情行为与替身一致，`reason` 里是引擎
自己铸的策略句柄（替身伪造不出来），而且重启后 `refund(500)` 仍然 ALLOW、
`refund(777)` 仍然 DENY —— 历史确实从快照/日志里恢复了。

### 真引擎逼出来的两处 schema 修正

只有真引擎会做类型检查，替身永远不抱怨。第一次拿真引擎装这份策略时撞到两个错：

1. `mentions field output.approved, which is not declared on that event` —— 工具的
   **返回值**也要在 action schema 里声明。默认事件 schema 给 `response` 加了
   `...outputs(A)`，而 outputs 取的正是 `context.output`；只写 `context.input`
   的话，`output.*` 根本不存在。
2. `failed to resolve types: Boolean` —— Cedar schema 语法里布尔类型叫 **`Bool`**，
   不是 `Boolean`（Long / String / Bool / Set<T>）。

两处都已修在 `demo_schema.cedarschema` 里。

如果 `policy install` 报类型错误，多半是事件 schema（`.dwschema`）没声明策略要读的
字段 —— 那是 service schema 那一半，`--event-schema` 传进去，默认 schema 覆盖不到
你自己发明的字段。

## 文件

| 文件 | 作用 |
|---|---|
| `dogwood_client.py` | 协议客户端（长度前缀 + JSON），逐字对应 `dogwood-server/src/protocol.rs` |
| `dogwood_middleware.py` | `DogwoodGuard`（LangChain middleware）+ 不依赖框架的 `guarded_tool_call` |
| `run_demo.py` | DeepAgents 端到端剧情 |
| `test_middleware.py` | 拦截器断言（含 fail-closed 检查） |
| `demo_policy.dw` | 策略：审批永远允许；退款要有一小时内的同额批准 |
| `demo_schema.cedarschema` | action schema（Cedar 现成格式，不是 Dogwood 新造的） |
| `dogwood_stub_server.py` | 离线替身，只为让 demo 在没 Rust 时能跑；**别拿去当引擎** |
| `doctor.py` | 体检：依赖、API、socket、策略集、探针往返 |
| `requirements.txt` | 已验证的依赖版本 |

## 调试

按"先看现象、再看帧"的顺序来：

**1. 先跑体检。** `python3 doctor.py` 会逐项检查 Python 版本、四个包的版本、
钩子 API 是否还在、socket 是否存在且是 socket、`ping`/`status` 通不通、策略集是不是
空的。每一项失败都直接给出该执行的命令，退出码非 0 便于放进脚本。

```bash
python3 doctor.py --stub              # 连 Rust 都不用，先证明链路是通的
python3 doctor.py                     # 查默认路径 /tmp/dogwood-demo
python3 doctor.py --socket /tmp/dw/run/dogwood.sock --control /tmp/dw/control.sock
```

**2. 看每一帧的原文。** 协议问题全靠这个：

```bash
DOGWOOD_TRACE=1 python3 test_middleware.py
```

它把发出去和收回来的 JSON 原样打到 stderr，一眼能看出是动作名拼错、`logged`/
`context` 装错袋子，还是响应根本不是 `decision`。

**3. 看拦截器日志。** 默认 INFO 就会打出 `ALLOW refund({'amount': 500})`、
`DENY ...`、`history approve: recorded`。`--quiet` 关掉；`history ...: recorded`
那一行**必须**在放行之后出现，它是"历史真的记进去了"的唯一证据。

常见症状对应表：

| 现象 | 原因 | 怎么办 |
|---|---|---|
| 所有请求都 DENY，`reason` 是空 | 动作名和工具名对不上（最常见：大小写）；或策略里没有覆盖该动作的 permit | `DOGWOOD_TRACE=1` 看发出去的 `action` 字符串，跟 `.dw` 里逐字比 |
| 策略集是空的 | `policy install` 没跑或失败 | `dogwood-server ... status --json`，`rule_count` 应为 2 |
| 第一次放行后就一直拒 | `response` 事件没记上 | 日志里找 `history ...: recorded`；没有就是 `_after` 那步断了 |
| 历史有了但 `output.approved` 匹配不上 | 工具结果没还原成结构化 JSON | 看 `_tool_output()`；`DOGWOOD_TRACE=1` 看 `logged.output` 是对象还是字符串 |
| `connect ... No such file` | 服务没起 / 路径不对 | 先起服务，或 `doctor.py --stub` 确认代码没问题 |
| `Permission denied` | 控制面 socket 是 0700，你的 uid 不是属主 | 用服务端属主的 uid 跑，或换 `--dir` |
| `unknown field(s) in event: ['ts']` | 客户端想自己定时间戳 | 别传时间戳，store 会赋（这是故意的设计） |
| 长连接用一会儿就断 | 服务端对空闲连接有 30 秒读超时 | 重连，或别让连接闲着 |
| `policy install` 退出码 2 | 策略/schema 没通过校验（1 = 用法或 IO 错） | 拿语言仓库的 `dogwood validate` 单独验一遍 |

## 三件容易踩的事（这个 demo 自己全踩过一遍）

1. **动作名必须逐字等于工具名。** 第一版策略写的是 `Action::"Refund"`，工具叫
   `refund`，于是**所有**请求都被拒 —— deny by default 让"名字对不上"长得和
   "策略写错了"一模一样。工具名是 `refund`，策略里就只能是 `Action::"refund"`。
2. **`response` 事件不能省。** 只做调用前那一次 submit，`formerly` 那个窗口永远
   是空的，表现同样是"全都拒绝"。历史是你喂进去的。
3. **工具结果要还原成结构化 JSON。** LangChain 会把返回值包成 `ToolMessage`
   （结构化结果在 `artifact`，否则是 JSON 字符串形式的 `content`）。直接把
   `ToolMessage` 塞进事件的 `output`，策略里的 `output.approved` 就永远匹配不上，
   于是"历史有了、裁决还是拒"。`_tool_output()` 就是干这个的。

还有一条是设计选择而不是坑：`DogwoodGuard(protected_tools={"approve", "refund"})`
只把这两个工具当受治理对象，别的工具走 `on_unprotected`（默认 `"deny"`）。
DeepAgents 默认还会给模型 `ls` / `read_file` / `write_file` / `edit_file` / `glob` /
`grep` / `execute` / `task`，本 demo 的剧本没调用它们。想真在生产里用这些工具，
正确做法是在 schema 和策略里**显式建模并放行**，而不是让拦截器悄悄跳过。

## 生产前必须自己补的

- **数据面不认证调用方。** `peer_cred` 只在控制面被读，事件里的 `principal`
  完全由客户端自称，数据 socket 是 0666。所以这个 demo 的
  `principal='AgentCore::User::"alice"'` 是**演示用**；真环境要让可信网关注入身份，
  或者一租户一实例把边界交给进程和 uid。
- **裁决之外的一切都在你这边**：真的执行裁决（这个文件在拦）、审计日志（引擎日志
  会被 checkpoint 裁剪、裁决结果不落盘）、store 文件权限、主机时钟。
- **`dogwood-server` 自己标了 not for production**，它是参考实现；生产要照着它的
  信任模型重写服务层。
- **吞吐**：每个事件一次 fsync，单实例约 845 events/s、~1.18 ms 地板。工具调用这个
  频率够用，别拿它记高频遥测。
