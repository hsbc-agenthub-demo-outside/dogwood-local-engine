# Dogwood AgentHub 演示工具包

把 [Dogwood](https://github.com/dogwood-policy/dogwood)（AWS 开源的 agent 治理语言）
接到 agent 框架的**工具调用边界**上，并配一套可运行的调试工具：一条命令跑真引擎、
9 个银行场景策略、逐条断言的验证脚本。

> **归属声明**：本仓库是独立项目，**不是 AWS 官方仓库，与 AWS 无隶属关系**。
> 仓库内的 Rust 源码来自 AWS 开源项目 `dogwood-policy/dogwood-local-engine`
> （Apache-2.0，版权与许可见 [`NOTICE`](NOTICE) 与 [`LICENSE`](LICENSE)）；
> 策略示例的写法参考了 AWS Open Source Blog 的文章
> [Introducing Dogwood: runtime verification for AI agents](https://aws.amazon.com/cn/blogs/opensource/introducing-dogwood-runtime-verification-for-ai-agents/)。
> 上游英文 README 原文保留在 [`README.upstream.md`](README.upstream.md)。

---

## 这个仓库在做什么

Dogwood 只做一件事：**根据历史回答"这个请求该不该放行"**。它不拦截、不执行工具、
也不认证身份——那些是宿主（你的 harness / middleware）的责任。

本仓库把这条链路完整搭出来并跑通：

```text
agent（DeepAgents / ADK / 自研）
  └─ middleware（本仓库提供）        ← 拦截发生在这里：拿到 DENY 就不调用工具
        │  Unix socket（长度前缀 + JSON）
        ▼
  dogwood-server（Rust）             ← 维护历史、给出裁决
        ▲
        │
  policy.dw + schema.cedarschema     ← 策略与动作 schema
```

## 三分钟跑通

### 0. 前置

| 需要什么 | 干什么用 | 备注 |
|---|---|---|
| Rust 工具链 | 构建引擎服务端 | `curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs \| sh` |
| Python 3.11+ | 跑客户端与场景 | |
| `python-demo/requirements.txt` | 仅跑 **agent 集成** demo 才需要 | langchain / deepagents 等 |

```bash
git clone git@github.com:hsbc-agenthub-demo-outside/dogwood-local-engine.git
cd dogwood-local-engine
cargo build --release -p dogwood-server        # 首次约 40 秒
```

### 路径 A：只调策略（不需要任何 LLM 依赖）

只用 Python 标准库，适合专心调策略、看裁决变化：

```bash
cd python-demo

# 一次性：生成"装策略"的小工具，并跑通真引擎全链路（含重启恢复验证）
DW_HELPER_DIR=~/dw-install DW_STORE_DIR=~/dw-store ./test_with_real_engine.sh

# 9 个场景：列出 / 单跑 / 全跑（约 8 秒）
python3 -m hsbc.run_hsbc --list
python3 -m hsbc.run_hsbc 01
python3 -m hsbc.run_hsbc --all
```

加 `--keep` 会保留该场景的 store 目录（含 `server.log`），方便手工接着玩：

```bash
python3 -m hsbc.run_hsbc 05 --keep
```

### 路径 B：接入 agent（DeepAgents / LangChain）

```bash
cd python-demo
python3 -m pip install -r requirements.txt

python3 doctor.py --stub --deep --probe    # 体检：依赖版本、API、socket、策略集
python3 test_middleware.py                 # 拦截器行为断言（不经过 LLM）
python3 run_demo.py                        # 三幕剧情：先拒 → 记批准 → 放行
```

`run_demo.py` 默认用进程内的**离线替身**（`dogwood_stub_server.py`：同协议，
但**不是**引擎实现），所以不需要 API key、结果可复现。要打真引擎就跑上面路径 A 的脚本。

## 平台差异（先看这个，能省一小时）

| 能力 | Linux | macOS / 其他 |
|---|---|---|
| 数据面：提交事件、拿裁决 | ✅ | ✅ |
| 控制面：在线安装 / 修改策略 | ✅ | ❌ 一律拒绝 |
| 装策略的方式 | `dogwood-server policy install`（在线、原子替换） | 停服务 → 用引擎库离线装 → 再起服务 |
| 集成测试 | 全绿 | `cargo test -p dogwood-local-engine` 全绿；23 个**控制面**测试必失败 |

原因：控制面的信任边界依赖 Linux 的 `SO_PEERCRED`（内核认证的对端身份）。非 Linux 上
`peer_cred()` 直接报错——**fail-closed**：宁可服务不可用，也不在没有边界的平台上"能用"。
所以 macOS 上走 `test_with_real_engine.sh` 里那条"用库离线装策略"的路（脚本已自动化）。

## 九个场景（`python-demo/hsbc/`）

每个场景 = 一个独立的 `.dw` 策略文件 + 一段事件轨迹 + 逐条断言，用**真引擎**跑，
可以单独改、单独验：

| # | 规则 | 时间机制 |
|---|---|---|
| 01 | 大额转账必须有同额批准（maker-checker） | `formerly within 1h` |
| 02 | 一小时内最多 5 笔转账 | `count_within` + `::request` |
| 03 | 一小时累计不超过 50000 | `sum_within` |
| 04 | 单笔不得超过已结算总额（反异常） | `bind` + `::response` |
| 05 | 读过机密客户数据后 8 小时内禁止对外转账 | `formerly` + 输入过滤 |
| 06 | 一小时最多 3 个不同收款人 | `count_distinct_within` |
| 07 | 制裁名单硬禁（纯 Cedar） | `forbid` 覆盖 `permit` |
| 08 | 会话有效性：验证后未再失败 | `since` + 否定左操作数 |
| 09 | 一次批准只能消费一次（进阶） | 聚合对聚合 |

细节与"六个必须知道的语义"见 [`python-demo/hsbc/README.md`](python-demo/hsbc/README.md)。

## 五个核心概念

| 概念 | 一句话 |
|---|---|
| 两种事件 kind | `request` 是**决策点**（拿 allow/deny），`response` 是**历史**（只入账） |
| `logged` vs `context` | `logged` 是**给未来**的历史记录，`context` 是**给现在**的请求字段。策略里 `input.amount: context.input.amount` 读作"历史那笔的金额 == 当前这笔的金额" |
| 历史按 principal 分键 | 每条事件（**包括 response**）都要带 `principal`；漏了它历史会落到别的键上，症状是**静默 DENY** |
| deny by default | 没有 `permit` 覆盖就是拒绝；`forbid` 覆盖 `permit` |
| 裁决 ≠ 执行 | 引擎只回裁决，**拦截由 middleware 做**（不调用工具） |

更完整的机制说明（含"为什么必须发 response 事件"）见
[`python-demo/dogwood_middleware.py`](python-demo/dogwood_middleware.py) 的模块文档。

## 常用命令

| 想干什么 | 命令 |
|---|---|
| 构建服务端 | `cargo build --release -p dogwood-server` |
| 引擎测试（macOS 可全绿） | `cargo test -p dogwood-local-engine` |
| 全量测试 | `cargo test --workspace`（macOS 上 23 个控制面测试必失败） |
| 起服务 | `./target/release/dogwood-server --dir /tmp/dw run` |
| 手工发一个事件 | `./target/release/dogwood-server --dir /tmp/dw submit HSBC::Action::transfer request --principal 'HSBC::User::"alice"' --resource 'HSBC::Account::"main"' --logged '{"input":{"amount":1000}}' --context '{"input":{"amount":1000}}'` |
| 看运行态 | `./target/release/dogwood-server --dir /tmp/dw status --json` |
| 真引擎跑通全链路 | `cd python-demo && DW_HELPER_DIR=~/dw-install DW_STORE_DIR=~/dw-store ./test_with_real_engine.sh` |
| 跑 9 个策略场景 | `cd python-demo && python3 -m hsbc.run_hsbc --all` |
| 体检 / 排障 | `python3 doctor.py` · `DOGWOOD_TRACE=1 python3 test_middleware.py`（打出每一帧 JSON） |
| 起 HTTP + Swagger（Docker） | `cp .env.example .env` → 填 `DOGWOOD_CONTROL_TOKEN` → `docker compose up --build -d` → <http://127.0.0.1:8080/docs> |
| HTTP 全链路自检 | `./dogwood-http/examples/smoke.sh` |
| HTTP 测试用例集（写规则 + 验证裁决） | `python3 dogwood-http/examples/testcases.py --all`（加 `-v` 看每次 allow/deny） |

## HTTP 面（`dogwood-http/`，可选）

引擎的接口是 Unix socket 上的长度前缀 JSON。如果部署环境拿不到 Unix socket
（Docker Desktop、远程 harness、或者只想点点按钮看裁决），
[`dogwood-http/`](dogwood-http) 把它翻成 HTTP，并给两个面各配一份 OpenAPI + Swagger UI：

```bash
cp .env.example .env
echo "DOGWOOD_CONTROL_TOKEN=$(openssl rand -hex 32)" > .env
docker compose up --build -d
./dogwood-http/examples/smoke.sh     # 真引擎：403 → 记录批准 → 200；10 条断言
```

如果只想要**单容器 + 一条 `docker run`**（不需要 Compose），用
[`Dockerfile.allinone`](Dockerfile.allinone)：它把引擎和两个面打进一个容器、一个 uid，
构建与运行见 [`docs/单容器部署（all-in-one）.md`](docs/单容器部署（all-in-one）.md)。
代价是数据面与控制面同进程（失去防篡改的 uid 边界），适合单机 / 单租户 / 可信 agent。

两条要记住的：**deny 是 HTTP 403，而不是 `200 + allowed:false`**（于是「看 2xx 就
放行」的客户端天然 fail-closed）；**控制面必须和引擎同 uid、同 PID namespace**
（control socket 是 `0700`，且 peer 认证依赖 Linux 的 `SO_PEERCRED`）。
详见 [`dogwood-http/README.md`](dogwood-http/README.md)，里面也记了两个上游/环境层面的
真实约束：数据 socket 被 `0700` 的 state 目录挡住，跨 PID namespace 的 peer 认证会失败。

## 已知限制

- **控制面仅 Linux**（见上表）。`dogwood-server` 自标 **not for production**：它是参考实现。
- **窗口必须有界**（默认上限 24h），"从此永远"只能用足够长的窗口近似。
- **时序子语言没有 `||`**，只有 `&&` 和 `!`；"或者"要用多条规则表达。
- **聚合只有 `count` 和 `sum`**（无 min/max/avg）；`count_within` 这类是标准库宏。
- **单机单写者**：一个 store 同一时刻只能被一个进程打开。
- **没有监控/审计**：裁决结果不落盘、内部日志会被 checkpoint 裁剪；审计与指标必须宿主自己建。
- **不认证、不执行**：事件里的 `principal` 由调用方填写，引擎无法验证真假。

## 目录结构

| 路径 | 说明 |
|---|---|
| [`dogwood-local-engine/`](dogwood-local-engine) | Rust 引擎（上游源码）：持久化、增量的时序鉴权引擎 |
| [`dogwood-server/`](dogwood-server) | Rust 服务端（上游源码）：把引擎放在 Unix socket 后面 |
| [`dogwood-performance/`](dogwood-performance) | Rust 基准（上游源码） |
| [`python-demo/`](python-demo) | **本仓库的主要交付**：Python 客户端、拦截器、demo、体检脚本 |
| [`python-demo/hsbc/`](python-demo/hsbc) | 9 个银行场景策略 + 轨迹 + 断言 |
| [`README.upstream.md`](README.upstream.md) | 上游 README 原文（保留以便对照） |
| [`AGENTS.md`](AGENTS.md) | 仓库协作与构建约定 |

## 许可与归属

- 本仓库整体以 **Apache-2.0** 发布。`dogwood-local-engine/`、`dogwood-server/`、
  `dogwood-performance/` 源自 AWS 的开源项目，版权归 Amazon.com, Inc. 或其关联方所有
  （见 [`NOTICE`](NOTICE)）——请保留这些文件。
- 上游项目：<https://github.com/dogwood-policy/dogwood>（语言、指南、参考解释器）与
  <https://github.com/dogwood-policy/dogwood-local-engine>（本仓库包含的引擎）。
- 本仓库的示例与工具由本仓库贡献者维护；上游不接受本仓库的 PR（上游是只读镜像）。
