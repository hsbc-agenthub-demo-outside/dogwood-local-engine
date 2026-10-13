# OPA 与 Dogwood：全面对比、效果评估与部署建议

> **文档范围**：对比 [Open Policy Agent (OPA)](https://github.com/open-policy-agent/opa) 与本仓库所承载的
> Dogwood（Rust 引擎 `dogwood-local-engine` + 服务端 `dogwood-server` + 适配器 `dogwood-http`，
> 语言前端来自 [dogwood-policy/dogwood](https://github.com/dogwood-policy/dogwood)）。
>
> **信息来源**（均为本文写作时抓取的真实源码/文档，非二手转述）：
> - OPA 仓库 `main` 分支快照（提交 `89ed3cf2`，2026-10-12），含 `README.md`、`docs/docs/*`、`server/`、`plugins/`、`go.mod`。
> - 本仓库 `README.md`、`README.upstream.md`、`dogwood-local-engine/README.md`、`dogwood-server/src/lib.rs`、
>   `dogwood-http/README.md`、`dogwood-performance/README.md`、`docker-compose.yml`。
> - 上游语言仓库 `dogwood-policy/dogwood` 的 `README.md` 与 `dogwood-docs/guide/*`。
>
> **一句话定位差异**：OPA 是一个**通用、无状态、以请求为输入**的策略决策点（PDP）；
> Dogwood 是一个**为 AI Agent 工具调用治理设计、自带事件历史与时间窗口**的有状态授权引擎。

---

## 0. 先看结论（TL;DR）

| 问题 | 结论 |
|---|---|
| 它们是不是同类替代品？ | **不是**。重叠区间只有"授权/策略裁决"这一点；OPA 覆盖远得多（基础设施、K8s、API、CI/CD），Dogwood 纵深深得多（跨时间的历史推理）。 |
| 最根本的技术差异是什么？ | **OPA 每次决策只看这一条 `input`；Dogwood 的引擎持有事件流，按时间窗口对历史求值。**前者是"纯函数"，后者是"带状态的流处理器"。 |
| 谁更成熟？ | OPA 是 **CNCF 毕业项目**、有第三方安全审计、海量生产用户；Dogwood 是 **AWS 新开源**的项目，参考解释器明说"非生产用"，本地引擎是 1.0 但生态很薄。 |
| 该不该都部署为服务？ | **OPA：应该，且默认做成"本地 sidecar/daemon 服务"**（Go 程序可退化为内嵌库，非 Go 可用 Wasm）。<br>**Dogwood：不应该做成网络服务**；优先"进程内嵌库"，仅当 agent 不可信、需要防篡改边界时，才用 `dogwood-server` 做**本机 Unix socket 独立进程**。`dogwood-http` 只是适配器，不是生产网关。 |
| 能不能一起用？ | **能，而且是推荐姿势**：OPA 管"基础设施/API/集群"这类无状态通用策略，Dogwood 管"agent 工具调用的历史时序"这类有状态治理。二者**控制面必须保持隔离**，不要合进一个进程。 |

---

## 1. 两个项目速览

| 维度 | OPA (Open Policy Agent) | Dogwood |
|---|---|---|
| 发起方 | CNCF（毕业项目），创始团队来自 Styra | AWS 开源（`dogwood-policy/*`） |
| 定位 | 通用策略引擎（general-purpose policy engine） | 面向 AI Agent 与工具调用的治理语言 + 授权引擎 |
| 策略语言 | **Rego**（Datalog 派生，声明式，支持递归规则、内置函数、模块化） | **Dogwood**（Cedar 派生语法 `permit/forbid/when`，**外加时间条件**） |
| 实现语言 | Go | Rust（引擎/服务端）+ Rust（语言前端，编译到 Cedar） |
| 核心拓扑 | PDP：调用方每次提供 `input`，OPA 返回决策 | 引擎持有**事件历史**，对每个 `request` 事件给出裁决 |
| 状态性 | **无状态为主**：决策 = f(policy, data, input)。`data` 可由 bundle / 外部数据注入 | **有状态**：引擎按 principal 分键持久化事件流，维护每个时间窗口的增量状态 |
| 典型接入 | REST API（`localhost:8181`）、Go SDK、Wasm、语言 SDK | Rust 库（内嵌）、Unix socket 数据面/控制面、HTTP 适配器（demo） |
| 分发/管理面 | Bundles、Discovery、Status、Decision Logs、签名校验、插件系统 | 引擎内的策略 install/add/update/delete + 事件日志；**无签名、无发现服务、无内置审计输出** |
| 生产成熟度 | 极高（K8s Gatekeeper、Envoy/Istio、Terraform、Docker、SSH 等） | 早期。参考解释器与 `dogwood-server`/`dogwood-http` 均标注"非生产/demo"；引擎库是 1.0 |
| 许可 | Apache-2.0 | Apache-2.0 |
| 语言版本 | OPA v1.x 主线（v0 兼容层仍在） | `dogwood-language = 1.0.0`（git 依赖，按 `rev` 固定） |

---

## 2. 根本差异：无状态决策 vs 有状态（历史）决策

这是理解一切的基点，其它差异大多由此派生。

### 2.1 OPA：决策是一个纯函数

OPA 的决策签名本质上是：

```
decision = evaluate(policy_modules, data_documents, input)
```

- `input` **完全由调用方提供**，每次调用独立；
- OPA **不记得上一次决策**，也不保留"某个用户过去干过什么"这类事件流；
- 如果策略需要历史（比如"同一用户 1 小时内是否登录过"），**你必须自己在外部把这段历史查出来，塞进 `input` 或 `data`**；
- 你能用的"时间"是 `time.now_ns()` 这样的当前时间读数，**没有内建的窗口/事件保留机制**。

这带来极好的水平扩展性与可复制性（任意实例结果一致），代价是：**跨时间的规则要你在 OPA 之外自己搭**。

### 2.2 Dogwood：决策是一个流处理器

Dogwood 的决策签名更接近：

```
verdict(request) = evaluate(policy, monitor_state, event_history, now)
```

- 引擎**自己持有事件流**：`request` 事件是"决策点"（返回 allow/deny），
  `response` 事件只是"历史入账"（无裁决）；
- 引擎**自己给事件盖时间戳**（在 append 时分配，客户端不能伪造），append 的落盘顺序**就是**事件的时序；
- 每个时间条件（`formerly within 1h …`、`count_within`…）维护**只属于自己的窗口**，事件到达时增量更新，而不是每次重扫整段历史；
- 崩溃后从快照 + 日志重放恢复，**已确认的提交不会因崩溃丢失**。

一句话对比：

> **OPA 回答"以我现在看到的输入，按规则现在该不该放行"；
> Dogwood 回答"以这个 agent 之前真实发生过的所有事情，此刻该不该放行"。**

### 2.3 用一个策略把差异打出来

策略：*只有同一用户在最近 1 小时登录过，才允许 Read。*

**Dogwood**（引擎持有历史，条件直接写成时间窗）：

```text
@id("read_after_login")
permit (principal, action == Drupe::Action::"Read", resource)
when temporal {
    formerly within 1h Drupe::Action::"Login"::response{ input.user: context.input.user }
};
```

**Rego**（历史必须由调用方喂进来——注意 `input.history` 是你自己的责任）：

```rego
package authz

default allow := false

allow if {
    some i
    login := input.history[i]
    login.action == "Login"
    login.user == input.user
    input.now_ns - login.ts_ns <= 3600 * 1000000000
}
```

Rego 版本**能写**，但真正的工作量在于：谁保存 `history`、谁给 `ts_ns` 保证时序、
谁在窗口外清理、并发下谁保证顺序——这些在 Dogwood 里是引擎的内建职责，在 OPA 里是**你的应用职责**。

---

## 3. 语言与表达能力对比

### 3.1 OPA / Rego 的能力面

- **Datalog 派生 + 完整工程化**：集合/对象/数组、推导、`some`/`every`、规则分组、模块、包（namespace）；
- **递归规则**、部分求值（partial evaluation）、编译 API、IR/Wasm 目标；
- **丰富内置函数**：字符串/正则/JSON/YAML/CIDR/加密/JWT/时间/`http.send` 等；
- **外部数据**：bundle 内数据、`data` 文档、外部数据插件（如从数据库拉取）；
- **可测试、可格式化、可静态分析**：`opa test`、`opa fmt`、`opa check`、VS Code 插件、Playground；
- **演进中的语法**：v1 语法、`rego.v2` 导入（`and`/`or`/改进的 `not`）等；
- 策略可以表达**任意**（可判定的）规则，不局限于授权。

### 3.2 Dogwood 的能力面

- **Cedar 语法内核**：`permit`/`forbid` + `when`/`unless`，实体/动作/资源模型，类型化 schema；
- **时间子语言**（这是 OPA 没有的）：
  - 谓词与窗口：`formerly`（窗口内发生过）、`previous`（紧邻的前一事件）、`since`（自某锚点以来持续成立）；
  - 量词与时间点：`exists`、`tp`；
  - 聚合：`count`、`sum`（含 `count_within`、`sum_within`、`count_distinct_within` 等标准库宏）；
  - 字面量/变量绑定、字段模式（`{ input.user: context.input.user }` 这类相关性匹配）；
- **信息提供者（Information Providers）**：Rhai 脚本在求值时计算派生事实，注入为 `context.providers.*`；
- **编译到 Cedar**：时间/提供者字段降级为 `context.*` 槽位，由运行时填充；
- **可插拔后端**：策略引擎（本地 Cedar 或远程策略库）与时间引擎（内存或持久化）独立替换。

### 3.3 对照表

| 能力 | OPA / Rego | Dogwood |
|---|---|---|
| 通用布尔/集合/对象运算 | ✅ 很强 | ✅（Cedar 级别，够用但更"授权专用"） |
| 递归规则 | ✅ | ❌（编译到 Cedar，无递归；两者都保持可判定） |
| 字符串/正则/编码等内置函数 | ✅ 极丰富 | ⚠️ 较少，复杂计算走信息提供者（Rhai） |
| 网络/IO 内置（如 `http.send`） | ✅（谨慎使用） | ⚠️ 仅经 provider（`net` feature 下有 `http_get`，**无 SSRF 校验**） |
| **时间窗口 / 事件历史推理** | ❌ 无内建（要自建） | ✅ **核心能力** |
| **跨请求的持续状态** | ❌ 无（要自建） | ✅ 引擎内建、可持久化、可恢复 |
| 外部数据注入 | ✅ bundle / 插件 | ⚠️ 主要靠事件流 + entity 属性；无 bundle/发现机制 |
| 形式化语义/验证工具 | ✅ 有测试/静态分析 | ✅ 有 `validate`/`lower`/`replay`/形式化规范文档 |
| 生态/编辑器/Playground | ✅ 巨大 | ⚠️ 早期（有 CLI、指南、AI 辅助写策略的 skills） |

### 3.4 语言层面的已知限制（来自上游自述）

Dogwood 时间子语言**只有 `&&` 和 `!`，没有 `||`**；聚合只有 `count`/`sum`（无 min/max/avg）；
窗口必须**有界**（默认上限 24h），"从此永远"只能用足够长的窗口近似。
Rego 侧没有这些限制，但代价是那些"历史/窗口"语义要你自己在外部实现，且**没有引擎替你保证时序**。

---

## 4. 架构与运行时对比

### 4.1 OPA 的三种运行形态

1. **独立守护进程 / sidecar 容器**（最常见）：`opa run --server`，默认监听 `localhost:8181`，
   暴露 REST API（Policy API / Data API / Query API / Compile API / Health API / Config / Status）。
2. **内嵌库（Go SDK）**：`github.com/open-policy-agent/opa/v1/sdk` + `rego` 包，进程内求值、无网络跳数。
3. **Wasm**：`opa build -t wasm -e pkg/rule` 把策略编译成 Wasm，任意 Wasm 运行时内嵌（非 Go 语言首选）。
   注意：核心语言完全支持，但部分内置函数（如 `http.send`）需宿主实现。

管理面是 OPA 的另一半：**Bundles**（分发策略/数据）、**Discovery**（集中下发配置与策略选择）、
**Status**（上报健康/激活状态）、**Decision Logs**（决策审计上报）、**插件系统**（bundle/rest/status/logs 等）。

### 4.2 Dogwood 的三种形态

1. **引擎库 `dogwood-local-engine`（推荐嵌入）**：直接在 harness 进程内 `open` store、`install` 策略、`submit` 事件。
2. **`dogwood-server`（进程边界）**：把策略集、编译后的监控器、事件日志放到 OS 进程边界之后，
   通过两个 **Unix socket** 提供服务：
   - **数据面**：`DataRequest`（提交事件）——类型系统里**没有**任何"改策略"的变体；
   - **控制面**：`ControlRequest`（install/add/update/delete/status/checkpoint）——用内核 peer credential（`SO_PEERCRED`）+ uid 白名单鉴权；
   - 线路格式：4 字节大端长度前缀 + JSON，单帧上限 16 MiB。
3. **`dogwood-http`（部署适配器，非引擎）**：把两个 socket 翻成 HTTP + OpenAPI + Swagger，
   用于拿不到 Unix socket 的环境（Docker Desktop、远程 harness）。**它不链接引擎、不打开 store。**

### 4.3 对照表

| 维度 | OPA | Dogwood |
|---|---|---|
| 默认对外协议 | HTTP REST（`:8181`） | Unix socket（长度前缀 JSON）；可选 HTTP 适配器 |
| 传输可远程化 | ✅ 天生 TCP/HTTP | ⚠️ 靠 `dogwood-http`；控制面**必须**与引擎同 uid + 同 PID namespace |
| 进程内嵌 | ✅ Go SDK / ❗Wasm（非 Go） | ✅ Rust 库（一等公民） |
| 多语言接入 | ✅ REST + 众多语言 SDK | ⚠️ 任意语言可手写 socket 客户端；本仓库有 Python |
| 策略热更新 | ✅ Bundle 轮询 / API 推送 / Discovery | ✅ `install`/`batch`（在线原子替换；**控制面仅 Linux**） |
| 控制面/数据面隔离 | ⚠️ 同一 REST 服务，按 API 分组；靠鉴权区分 | ✅ **物理分离的两个 socket + 两套动词集**，类型系统强制 |
| 持久化 | 内存；可选磁盘后端（Badger，文档标注"视为临时"） | ✅ redb 追加日志 + 快照，崩溃恢复 |
| 崩溃恢复语义 | 无状态，恢复即重启 | ✅ 已确认提交不丢，无法恢复的 store 会被拒绝而非带残缺历史打开 |

### 4.4 关键补充：Dogwood 的"进程边界"到底在防什么

`dogwood-server` 的存在不是为了性能，而是为了**防篡改**：
一个链接了引擎库的进程可以改写自己正在被裁决的策略集——你无法用一个"agent 能控制的规则"去治理 agent。
所以 `dogwood-server` 把策略集搬到进程边界之后，并且要求：

> **服务端必须跑在比被监控 agent 更低权限、不同的 uid 上。同 uid = 没有边界。**

`dogwood-http` 则明确承认自己**削弱**了这层安全：Unix socket 的 peer 认证换成 bearer token，
token 会泄漏、不会过期、认证的是"一个部署"而不是"一个人"。所以它默认只绑 loopback。

---

## 5. 状态、持久化与恢复对比

| 关注点 | OPA | Dogwood |
|---|---|---|
| 主要状态 | 策略 + `data` 文档（内存），`input` 每次传入 | 策略集 + **事件日志** + 每个时间条件的增量监控状态 |
| 持久化 | 默认纯内存；磁盘后端针对"装不下的数据"，且文档强调按临时数据处理、无备份/修复流程 | redb 嵌入式 ACID 存储；快照间隔可配；快照 + 重放恢复 |
| 历史/窗口 | **无**；由调用方在外部实现 | **有**：按 principal 分键，窗口有界 |
| 写者模型 | 无状态，天然多实例 | **单机单写者**：一个 store 同一时刻只能被一个进程打开 |
| 时间来源 | 调用方或 `time.now_ns()`（宿主时钟） | 引擎在 append 时盖时间戳，且**强制单调递增**（系统时钟回拨也钳制），但时钟本身仍需宿主保护 |
| 恢复正确性验证 | N/A | 有差分测试：恢复后的裁决必须与前端参考解释器一致（`tests/recovery_oracle.rs`、`corpus_diff.rs`） |

> 注意 OPA 磁盘存储文档的原话精神：它**不是**数据的事实来源，且不提供备份/恢复/修复流程——别把它当数据库用。
> Dogwood 反过来：它的 store **就是**事实来源之一，但**日志会被 checkpoint 裁剪**，不能拿它当审计日志。

---

## 6. 效果与性能

先说清楚：**这两者不是同一条流水线上的两个版本，跨实现的"谁快"对比很容易误导。**

### 6.1 OPA 的性能特征

- 官方主打 **linear fragment**：用线性片段的 Rego，评估接近常数时间；
- **规则索引（rule indexing）**：对 `==`、`glob.match`、`startswith`/`endswith` 等语句做索引，规则越多不一定越慢；
- 通用建议：**用对象而非数组**做按 ID 查找、把复杂策略保持在线性片段内；
- 典型 API 授权决策在**亚毫秒到个位数毫秒**量级，取决于策略复杂度、`data` 大小与是否触发外部数据/builtin；
- 决策日志是**异步上报**的，通常不进入决策延迟关键路径；
- 无状态 → 水平扩展天然友好，容量问题主要是 CPU/内存。

### 6.2 Dogwood 的实测（来自本仓库 `dogwood-performance/README.md`）

测试环境：16 核 Xeon 8259CL @ 2.50GHz，NVMe/ext4，load ~0.2。**机器相关，引用前请重测。**

**持久化地板**：每次 fsync 追加 ≈ **1.18 ms**，单实例上限 ≈ **845 events/s**。
这是"数据面必须先把事件落到盘再回答"的硬地板，任何"OPA 更快"的直觉对比都要先减掉它。

每次决策延迟（200 条历史事件）：

| 策略数 | 参考解释器（内存、非持久化） | `dogwood-server`（持久化） | 比值 |
|---|---|---|---|
| 10 | **0.30 ms** | 1.23 ms | 参考快 4.1× |
| 100 | 2.30 ms | **1.70 ms** | server 快 1.4× |
| 1000 | 22.3 ms | **6.45 ms** | server 快 3.5× |
| 10 000 | 233 ms | **72.4 ms** | server 快 3.2× |

三条结论：

1. **交叉点落在 10–100 条策略之间**。10 条时两者的 compute 其实相当，差距基本就是那 1.18 ms 的 fsync 地板。
2. **超过交叉点后，server 赢在算法而非存储**：参考解释器每次决策重扫历史（策略 1000×，延迟涨 ~780×），
   server 增量求值（只涨 ~59×），**同时还多做了持久化**。
3. 到 10 000 条策略，**评估完全主导**（72 ms 是 fsync 地板的 ~61×），增量 vs 重扫的差异最干净。

并发（5s 开环，1–500 rps）：

| 策略数 | 供给速率 | 实际达成 | p50 | p99 | 状态 |
|---|---|---|---|---|---|
| 10 | 100 | 100 | 1.48 ms | 3.53 ms | 健康 |
| 10 | 500 | 500 | 4.00 ms | 10.7 ms | 接近容量 |
| 100 | 500 | **246** | 1653 ms | 5051 ms | **饱和** |
| 1000 | 100 | **97** | 9.08 ms | 171 ms | **临界** |
| 1000 | 500 | **35** | 22 825 ms | 64 679 ms | **崩溃** |

要点：

- **现实区间（1–100 rps、≤1000 策略）很舒服**：p50 1.5–9 ms，p99 个位到低两位数毫秒。对"每个动作一次决策"的 agent 完全够用；
- **容量由评估决定，不是 fsync 天花板**：地板预测 ~835 rps，实际容量随策略数**下降**；
- **超过容量会出现经典排队崩溃**：吞吐**下降**、延迟涨到秒级——这也是为什么该 harness 用开环（闭环会自我限速，掩盖悬崖）。

其它实测：策略安装（1000 条 ≈ 160 ms，可交互；10 000 条 ≈ 1.77 s，操作员能感知的停顿）；
**pinning**（按 key 分片过滤）在"宽谓词 + 多会话"场景最高约 **2.8×**，在"高选择性谓词"上是纯开销。

### 6.3 如何公平地看待两者

- OPA 的 `input` 里通常没有"这段历史"——如果 Dogwood 的策略搬到 OPA，你**得先把历史查出来塞进 `input`**，
  查询/序列化的成本会回到你的应用里，OPA 那份延迟数字并不包含它；
- Dogwood 的 server 每次都**同步落盘**（回答前 fsync），OPA 默认无持久化副作用——比"裸延迟"不公平，要先减地板；
- 反过来，OPA 有成熟索引与线性片段，**纯无状态规则的评估**通常比 Dogwood 的时序求值便宜；
- 结论：**不要用"谁毫秒数小"选型**，用"哪类问题"选型（见第 10 节）。

---

## 7. 安全模型与信任边界

两者都**只发裁决、不执行、不认证**（这是共同点），但周边能力差距很大。

| 信任边界相关能力 | OPA | Dogwood |
|---|---|---|
| 是否认证调用方 | ❌ 不认证（应用/网关负责） | ❌ 不认证（`principal` 由调用方填写，引擎无法验证） |
| 是否执行裁决 | ❌ 只回决策，PEP 执行 | ❌ 只回裁决，middleware 执行 |
| 自身管理 API 的访问控制 | ✅ 可配鉴权（bearer/basic/TLS/mTLS）、TLS、RBAC 等 | ⚠️ 控制面靠 Linux uid/SO_PEERCRED；HTTP 适配器退化为单 bearer token |
| 策略完整性 | ✅ **签名 bundle** + 校验；TLS 传输 | ❌ 无签名机制；靠 store 文件权限与进程边界 |
| 决策审计 | ✅ **Decision Logs**（含 input/result/bundle 版本，可上报） | ❌ 不落盘裁决；内部日志被 checkpoint 裁剪，审计需宿主自建 |
| 策略/配置分发 | ✅ Bundle + Discovery + Status | ⚠️ 引擎内 install/batch（原子替换、失败则旧策略继续服务） |
| 第三方安全审计 | ✅ Cure53 报告（仓库内 `SECURITY_AUDIT.pdf`） | ❌ 未见公开审计 |
| 默认失败姿态 | 由你写规则决定（`default allow := false` 是惯例） | **fail-closed**：无 `permit` 即拒；`forbid` 覆盖 `permit`；评估出错即拒 |

**Dogwood 官方列出的、嵌入方必须自己承担的安全责任**（逐条都是真实约束）：
事件完整性、事件来源认证、裁决执行、store 文件与内存访问控制、控制面授权、**宿主时钟保护**、
独立审计日志、以及"必须捕获所有相关事件"（漏发 response 事件会让窗口永远为空，症状是**静默 DENY**）。

**Dogwood 在 agent 治理上的独有安全价值**：控制面与数据面**物理分离 + 类型强制**，
被监控 agent 的词汇表里**结构上不存在**"改策略"这件事；再配合 uid 隔离即得到内核级的防篡改边界。
OPA 也能做（同一 REST 服务里靠鉴权区分政策 API 与数据 API），但分离是"配置"而非"结构"。

---

## 8. 运维、可观测性与生态

| 维度 | OPA | Dogwood |
|---|---|---|
| 健康检查 | ✅ `/health` + Prometheus 指标 | ⚠️ `ping`（数据面 liveness）；**无内建指标**，监控需宿主自建 |
| 日志 | ✅ 可配级别（含新的 `--log-level-request`）；决策日志 | ⚠️ 有内部日志，但会被 checkpoint 裁剪，不能当审计 |
| 状态上报 | ✅ Status API + Discovery | ⚠️ `status`/`status --json` |
| 部署编排 | ✅ 官方 K8s/Docker/AWS/GCP/Azure 指南、Helm、Gatekeeper | ⚠️ 仓库内有 `docker-compose.yml`（本地 Docker Desktop 演示） |
| 测试工具 | ✅ `opa test`/`fmt`/`check`/Playground/VS Code | ✅ `dogwood validate`/`lower`/`replay` + Rust 差分/恢复测试 |
| 生态集成 | ✅ Envoy/Istio/K8s/Terraform/Docker/SSH/GraphQL/Kafka… | ⚠️ 早期，主要面向 agent harness（本仓库有 Python middleware/场景） |
| 多语言 SDK | ✅ 大量 | ⚠️ 本仓库提供 Python；其它语言手写 socket |

---

## 9. 成熟度、治理与许可

| 维度 | OPA | Dogwood |
|---|---|---|
| 治理 | CNCF 毕业项目，有 GOVERNANCE/MAINTAINERS | AWS 开源；上游是**只读镜像**，不接受外部 PR |
| 生产就绪度 | 极高，ADOPTERS.md 列出大量生产用户 | ⚠️ 参考解释器与 `dogwood-server`/`dogwood-http` 自标"非生产/demo"；本地引擎库 1.0 但生态/审计薄 |
| 安全审计 | ✅ Cure53 | ❌ 未公开 |
| 版本节奏 | 活跃，v1 主线持续演进 | 新项目，版本线为 1.0 |
| 许可 | Apache-2.0 | Apache-2.0 |

> Dogwood 的"非生产"标注值得认真对待：**可以被生产使用的是引擎库本身**，
> `dogwood-server`/`dogwood-http` 是**参考实现 / demo**，上生产需要自行加固（鉴权、审计、监控、时钟、uid 隔离）。

---

## 10. 决策矩阵：什么时候选谁

**选 OPA，如果**：

- 你要做的是**基础设施/平台类通用策略**：K8s 准入、Envoy/Istio 授权、API 网关、CI/CD、Terraform；
- 决策是**无状态的**：只看"这一次请求"的上下文（用户属性、资源、方法、路径、标签）；
- 你需要**成熟生态、签名策略分发、决策审计、多语言 SDK、Wasm/边缘**；
- 组织里已经有 OPA/Rego 能力与运维工具链。

**选 Dogwood，如果**：

- 你要治理的是 **AI Agent 的工具调用**，而规则**依赖这个 agent 的历史**：
  maker-checker（大额转账需同额批准）、单位时间限流（1 小时最多 5 笔）、
  "读过机密数据后 8 小时内禁止对外转账"、"一次批准只能消费一次"；
- 你希望**引擎替你维护事件历史、时间窗口、时序与崩溃恢复**，而不是自己在 OPA 外面搭一套；
- 你能接受"低权限 uid + Unix socket 进程边界"这种本机部署模型；
- 你能承担它的早期生态：审计/监控/签名/跨平台都要自己做。

**两者一起用（推荐组合）**：

- **OPA** 负责"环境/平台层"的无状态策略（谁能调哪个 API、哪个命名空间能跑什么）；
- **Dogwood** 负责"agent 行为层"的有状态时序治理（这个 agent 历史上下文中该不该做这一步）；
- 两者**各自独立部署、各自独立控制面**，通过 harness 编排：先问 OPA（环境许可），再问 Dogwood（历史许可），
  任一 DENY 即不执行工具。

---

## 11. 能否只保留一个：Dogwood 为什么不能完全替代 OPA

> 这是被问得最多的问题："既然 Dogwood 也能做授权，为什么不能把 `.dw` 策略包成 HTTP 接口，
> 直接顶替 OPA、只维护一个组件？"
> 简答：**在"依赖历史的 agent 授权"这个子领域，可以替代；在"通用策略引擎"这个超集上，不能。**

### 11.1 一句话结论

Dogwood 与 OPA 不是"新老两代"，而是**子集 / 互补**关系：

- Dogwood 能做、OPA 结构上做不了的：**跨事件历史的时序授权**；
- OPA 能做、Dogwood 覆盖不到的：**通用、无状态、输出任意决策文档、需要生态与横向扩展**的策略场景。

所以"只留一个"的判断标准不是"哪个更强"，而是**"你的策略到底需不需要历史"**。

### 11.2 你要的"HTTP + `.dw` 判断"，仓库里已经有了

`dogwood-http` 就是把两个 Unix socket 翻成 HTTP 的适配器（数据面 `POST /v1/events` +
控制面 `/v1/policy*`，各带一份 OpenAPI/Swagger）。所以"HTTP 化"不是缺口，真正的缺口在下面几节。

它也已经把代价写在明面上：

- 控制面从"内核 uid 认证"退化成 **bearer token**（会泄漏、不过期、认证的是"一个部署"而非"一个人"）；
- 控制面**仅 Linux 可用**（依赖 `SO_PEERCRED`），且必须与引擎同 uid、同 PID namespace；
- `deny` 是 **HTTP 403**；**504 是歧义的**（引擎可能已 append 也可能没有，且不做去重，不能盲目重试）。

### 11.3 四类硬约束（为什么"有 HTTP 接口"≠"能替代 OPA"）

#### 11.3.1 语言表达力（最本质，也最难补）

Dogwood 的内核是 **Cedar 派生 + 时间条件**，Cedar 本身是"授权专用"的可判定语言。具体差距：

| 能力 | OPA / Rego | Dogwood |
|---|---|---|
| 决策输出 | `result` 可以是**任意 JSON**（布尔/对象/集合/理由列表…） | 固定为 `{ allowed: bool, reason: [策略 token], errors, recorded_at_nanos }` |
| 拒绝理由可读性 | 可自定义（常直接回给 LLM，让 agent 改进） | `reason` 是**不透明策略 token**，回不了人话理由 |
| 输出型策略（如"哪些集群可部署"） | ✅ | ❌ 只能给 permit/deny 裁决 |
| 规则递归 / 推导 | ✅ | ❌（Cedar 无递归） |
| 内置函数 | regex / JSON / JWT / CIDR / 加密 / `http.send`… | 很少；复杂计算走 provider（Rhai） |
| 时间子语言自身 | N/A | 只有 `&&`/`!`（**无 `||`**）；聚合只有 `count`/`sum`；窗口必须**有界**（默认上限 24h） |

其中"**决策输出只能是布尔裁决**"这条最致命：OPA 那种"返回一个拒绝理由集合、把理由喂回 LLM"
的 agent 玩法，Dogwood 原生表达不出来（[`dogwood-http/src/dto.rs`](../dogwood-http/src/dto.rs)
里就是 `allowed + reason tokens` 这个形状）。

#### 11.3.2 性能与容量模型

- Dogwood 数据面**先把事件 fsync 落盘再回答**：实测地板 ≈ **1.18 ms/次、~845 events/s 单实例上限**，
  且随策略数下降（1000 策略在 100 rps 就临界）。对"每个 agent 动作一次决策"够用，
  但**无状态高频 API 授权（几千 rps）会被直接打穿**；
- **单机单写者**：一个 store 同一时刻只能被一个进程打开，**没有水平扩展故事**（只有 pin 分片一条路）；
- OPA 无状态，天然可复制、可水平扩，容量基本是 CPU/内存问题。

#### 11.3.3 管理面 / 运维（全要自建）

OPA 自带、Dogwood 没有的：Bundle 分发、Discovery 集中配置、**签名策略校验**、Status 上报、
**Decision Logs 决策审计**、Prometheus 指标、Wasm/边缘目标、多语言 SDK。
Dogwood 侧这些**全部要宿主自己造**——它的内部日志会被 checkpoint 裁剪、裁决不落盘，**不能当审计用**。

#### 11.3.4 生态与成熟度

OPA：CNCF 毕业、Cure53 安全审计、K8s Gatekeeper / Envoy / Istio / Terraform / Docker / SSH 等海量集成。
Dogwood：AWS 新开源，参考解释器与 `dogwood-server`/`dogwood-http` 都自标"demo / 非生产"。

### 11.4 "改造清单"：把 Dogwood 做成通用 HTTP 策略服务要补什么

若真要让 Dogwood 顶到 OPA 的位置，至少要补：

1. **任意 JSON 数据模型 + 规则输出任意值**（改语言与求值器，等于放弃 Cedar 的强验证优势）；
2. 完整**内置函数库**（regex / JSON / JWT / CIDR / 加密 / HTTP…）；
3. 策略**分发 / 签名 / 版本 / 回滚**（bundle + discovery + 签名校验）；
4. **决策日志 / 审计 / 指标**；
5. **TLS / mTLS / RBAC** 等管理面鉴权；
6. **水平扩展**（当前是单写者）；
7. **多语言 SDK**；
8. **跨平台**（控制面现在 Linux-only）；
9. **索引与性能工程**（对齐 OPA 的 linear fragment / rule indexing）。

把这份清单做完，你其实是在**重写一个 OPA**，同时还丢掉了 Dogwood 原本的强项——
用 Go + Rego 打磨十年的东西，用这种方式追赶并不划算。

### 11.5 什么情况下 Dogwood 单独就够

满足**全部**条件时，可以只用 Dogwood（HTTP 化已有）：

1. 策略范围就是**单个 agent 的工具调用治理**，判断依赖该 agent 的历史；
2. 决策只需要 **allow/deny**，不需要返回任意决策文档或人类可读理由；
3. 吞吐在**单机几百 events/s** 以内，不需要跨机水平扩展；
4. 能接受自己搭审计/监控，并接受控制面仅 Linux、需要 uid 隔离。

只要有一条不满足（尤其你还有基础设施/API/集群/CI 这类**无状态通用策略**），
就该 OPA 管通用面、Dogwood 管时间面。

### 11.6 小结

| 说法 | 是否成立 |
|---|---|
| "Dogwood 全面替代 OPA" | ❌ 不成立（子集 vs 超集） |
| "在依赖历史的 agent 授权上，Dogwood 替代 OPA" | ✅ 成立（需满足 11.5 的四个条件） |
| "把 `.dw` 包成 HTTP 就等于 OPA" | ❌ 只是传输层对齐，语言/输出/管理面/扩展性都不同 |
| "OPA 用 `input` 塞历史来替代 Dogwood" | ⚠️ 能写，但时序/并发/清理/恢复的正确性回到你的应用——这正是 Dogwood 存在的理由 |

---

## 12. 部署建议（重点）

### 12.1 OPA 该怎么部署

**默认推荐：本地 sidecar / daemon 服务，而不是远端集中服务。**

| 场景 | 推荐形态 | 理由 |
|---|---|---|
| Go 编写的服务 | **内嵌 Go SDK**（进程内）或本地 daemon | 内嵌零网络跳数；daemon 便于独立升级策略 |
| 非 Go 服务（Python/Java/Node…） | **本地 daemon / sidecar + REST** | 官方推荐，低延迟、断网可用、每实例自治 |
| 资源受限/边缘/IoT | **Wasm 模块内嵌** | 无需起 OPA 进程；注意部分 builtin 需宿主实现 |
| 大规模共享数据/批处理/Serverless | **集中式 OPA 服务** | 数据与资源可共享；但引入网络延迟与单点，需高可用 |

要点：

- **贴近 PEP**：OPA 官方建议尽量靠近被保护的服务，latency 与可用性都更好；
- **策略分发用 Bundle + Discovery**，别手动 ssh 上去改规则；
- **决策审计用 Decision Logs** 上报到中心，别只看本地日志；
- 如果确实要暴露 OPA 到网络：上 **TLS/mTLS + 鉴权**，并对管理类 API 做隔离——OPA 的 Policy/Data API 权限要靠你配置，它默认不替你认证。

### 11.2 Dogwood 该怎么部署

两种合法形态，按"agent 是否可信"选：

| 形态 | 何时用 | 代价 |
|---|---|---|
| **A. 进程内嵌引擎库** | agent/harness 与引擎在同一信任域，追求最简与最低延迟 | 没有防篡改边界：能改自己规则的进程无法被自己的规则治理 |
| **B. 独立进程 `dogwood-server`（推荐给不可信 agent）** | agent 可能不可信，需要策略/日志/监控器放在进程边界之后 | 必须 **不同 uid + 更低权限**；控制面**仅 Linux** |

形态 B 的硬性前置条件（`docker-compose.yml` 已经把这些约束写进注释）：

1. **服务端 uid ≠ agent uid，且服务端不高于 agent**——同 uid 就没有边界；
2. **控制面与引擎必须同 uid、同 PID namespace**：control socket 是 `0700`，别的 uid 在 `connect()` 就被拒；
   跨 PID namespace 时 `SO_PEERCRED` 会失败（内核报 pid=0）；
3. **store 目录是 `0700`，而 socket 又会因此不可达**——把 socket 目录单独做成一个卷/目录，只挂给数据面容器；
4. 数据面本身**默认无鉴权**（靠 loopback / 权限），别把数据 socket 暴露给不相关进程。

关于 `dogwood-http`（适配器）：

- **仅在拿不到 Unix socket 时使用**（Docker Desktop、远程 harness）；
- **控制面用 `--plane control` 单独进程**，和引擎同 uid/PID namespace；`--plane both` 只适合本地图省事；
- 记住它的契约：**deny 是 HTTP 403，不是 `200 + allowed:false`**——这是刻意的 fail-closed 设计；
  代价是 deny 会进 4xx 指标，看板要排除；
- **504 是歧义的**（引擎可能已 append 也可能没有，且不做去重）——**不要盲目重试**，拒绝这次动作，事后对齐；
- 控制面 bearer token **严格弱于**内核 uid 认证：会进日志/shell history、不过期、认证的是"部署"不是"人"；
  非 loopback 绑定必须显式承认（`--allow-non-loopback-control`）。

### 11.3 "是否两个都应该直接部署为服务？"

**明确回答：不应该用同一套思路把两者都做成网络服务。**

| | 是否做成服务 | 做成什么服务 | 关键边界 |
|---|---|---|---|
| **OPA** | ✅ **是，主流且推荐** | **本机 sidecar/daemon HTTP 服务**（Go 可退化为内嵌库） | 它与 PEP 同机；策略走 bundle/签名；管理面有鉴权 |
| **Dogwood** | ⚠️ **不默认**；多数场景**进程内嵌库**，需要防篡改时才是服务 | **本机 Unix socket 进程**（`dogwood-server`），可选 HTTP 适配器 | 服务端**更低权限、不同 uid**；控制面与数据面**物理分离** |

一句话：

> **OPA 服务化的目的是"多个 PEP 复用一个决策点"；Dogwood 服务化的唯一目的是"给同机不可信 agent 一个不可篡改的裁决者"。**
> 前者是**扩展性**诉求，后者是**安全边界**诉求——目标不同，别用同一套部署模板。
> 尤其：**不要把 `dogwood-http` 的控制面当成对公网/对网络的常规 REST 网关**，它只是把 Unix socket 抬到 HTTP 的适配器。

### 11.4 推荐蓝图 A：单机 agent 宿主（最贴合本仓库）

```
┌─────────────────────────── host ───────────────────────────┐
│  agent / harness (低权限 uid)                                │
│    └─ middleware ──► dogwood-server (数据面 socket)          │
│            │   DENY 则不调用工具                              │
│            └─ (可选) OPA sidecar :8181  ◄── REST 询环境策略    │
├─────────────────────────────────────────────────────────────┤
│  dogwood-server (引擎 uid, 独立进程; 0700 store)             │
│    └─ 控制面 socket (仅引擎 uid / 操作员 uid 可连)            │
└─────────────────────────────────────────────────────────────┘
```

- agent 的每次工具调用前：先（可选）问 OPA 环境许可，再问 dogwood 历史许可，**任一 DENY 即拒绝**；
- 工具返回后**必须发 `response` 事件**（否则 `formerly`/`since` 的窗口永远是空的，表现为静默 DENY）；
- 所有事件都要带 `principal`（历史按 principal 分键，漏了会落到别的键）；
- 审计日志由 harness 自己落（Dogwood 不做），OPA 的 Decision Logs 作为环境策略侧审计。

### 11.5 推荐蓝图 B：Kubernetes

- **OPA**：以 **sidecar**（与业务容器同 Pod）或 **Gatekeeper**（集群准入）部署；策略走 bundle(OCI/HTTP)+Discovery；
  对 OPA 自身的管理 API 做鉴权与网络策略限制。
- **Dogwood**：**不要把 `dogwood-server` 作为集群级共享服务**。它是有状态、单写者、按 principal 分键的，
  共享服务会同时破坏隔离与吞吐语义。正确做法是**每个 agent 实例一个本机引擎**（同 Pod 内独立容器或 sidecar，
  通过 Unix socket 通信），或直接把引擎库嵌进 harness 进程。
- 需要多活/分片时，走 Dogwood 提供的 **pin 分片**（每个分片自己的日志与 append 点），由路由层决定 key → 实例。

### 11.6 反面模式（不要这么做）

- ❌ 把 `dogwood-server` 当成一个集群共享的授权中心（破坏单写者/隔离语义）；
- ❌ agent 与 dogwood 服务端**同 uid**（等于没有进程边界）；
- ❌ 把 `dogwood-http` 的**控制面**（或 `--plane both`）暴露到非 loopback、无 TLS/token 保护的网络上；
- ❌ 把 Dogwood 的内部日志当审计日志（会被 checkpoint 裁剪，裁决不落盘）；
- ❌ 把 OPA 的磁盘存储当归档数据库（官方标注按临时数据、无备份/修复流程）；
- ❌ 在 OPA 里用 `input` 硬扛"历史时序"，却把时序/并发/清理的正确性留给业务代码（这正是 Dogwood 要解决的问题域）；
- ❌ 把时间语义分散两处：**一个决策如果需要历史，就让它只由 Dogwood 负责**，别一半在 OPA input 里拼历史、一半在 Dogwood 里再算一遍。

---

## 12. 如果两者一起用：分工原则

1. **按"决策是否依赖历史"切分**：
   只看当前请求 → OPA；依赖 agent 过去发生了什么 → Dogwood。
2. **按"状态所有权"切分**：谁需要状态，谁拥有状态。历史归 Dogwood 的 store；环境/静态/外部数据归 OPA 的 bundle/`data`。
3. **控制面隔离**：OPA 的策略下发（bundle/Discovery）与 Dogwood 的控制面（uid socket）**各走各的**，
   不要经由同一个进程/同一份凭据。
4. **失败姿态统一为 fail-closed**：两边的默认都应是"没拿到明确 allow 就不执行"。
   Dogwood 天然如此；OPA 侧用 `default allow := false`，并把"查询失败/超时"按拒绝处理。
5. **审计双写**：OPA Decision Logs（环境侧）+ harness 自己的工具调用审计（含 Dogwood 裁决）——两边合起来才是完整链条。

---

## 13. 对照速查表（一页版）

| | OPA | Dogwood |
|---|---|---|
| 本质 | 无状态通用 PDP | 有状态时序授权引擎 |
| 语言 | Rego（Datalog 派生） | Dogwood（Cedar 派生 + 时间条件） |
| 核心输入 | 每次的 `input` | 持续的事件流 + 时间窗口 |
| 时间/历史 | 自己做 | 内建 |
| 持久化 | 内存/可选磁盘 | 内建、可恢复 |
| 进程边界（防篡改） | 靠配置/鉴权 | 靠 uid + 双 socket（结构强制） |
| 策略分发 | Bundle/Discovery/签名 | 引擎内 install/batch（无签名/发现） |
| 审计 | Decision Logs | 无（宿主自建） |
| 生态/成熟度 | 极高 | 早期 |
| 推荐部署 | 本地 sidecar/daemon 服务（或内嵌/Wasm） | 优先内嵌库；不可信 agent 时用本机 socket 服务 |

---

## 附录 A：术语

- **PDP / PEP**：策略决策点 / 策略执行点。OPA、Dogwood 都是 PDP，都不做 PEP。
- **Rego**：OPA 的声明式策略语言。
- **Cedar**：AWS 开源的可判定授权语言，Dogwood 的语法内核。
- **Bundle**：OPA 分发的策略/数据包（可签名）。
- **Decision Log**：OPA 的决策审计流。
- **Control plane / Data plane**：改策略 / 提交事件与取裁决，两者在 Dogwood 里物理分离。
- **Pinning**：Dogwood 按 key 分片过滤历史，用于多会话隔离与加速（宽谓词下最高 ~2.8×）。

## 附录 B：参考链接

- OPA 仓库：<https://github.com/open-policy-agent/opa>
- OPA 文档：<https://www.openpolicyagent.org/docs>
- OPA 部署：`docs/docs/deploy/index.mdx`（本机抓取的仓库快照）
- OPA 集成与 SDK/Wasm：`docs/docs/integration.md`、`docs/docs/wasm.md`
- OPA 管理面：`docs/docs/management-bundles/`、`management-discovery.md`、`management-decision-logs.md`
- OPA Agent 工具调用：`docs/docs/agent-tool-calling.mdx`
- Dogwood 语言仓库：<https://github.com/dogwood-policy/dogwood>
- Dogwood 指南：<https://dogwood-policy.github.io/dogwood/>
- Dogwood 本地引擎仓库：<https://github.com/dogwood-policy/dogwood-local-engine>

## 附录 C：数据与结论的适用范围

- 性能数字来自 `dogwood-performance/README.md` 的实测表，**机器相关**（16 核 Xeon 8259CL、NVMe/ext4），引用前请重测；
- OPA 的性能描述以官方"linear fragment / rule indexing"文档与通用工程经验为准，**未在本文中做端到端 benchmark**；
- OPA 版本以写作时仓库快照（v1 主线，提交 `89ed3cf2`，2026-10-12）为准；
- Dogwood 版本以 `Cargo.lock` 的 `dogwood-language = 1.0.0`（rev `996d756`）与本仓库源码为准。

> 本文档是对比与建议，**不构成 OPA 或 Dogwood 官方文档**。二者行为的最终依据是各自仓库与官方文档。
