# Dogwood 调研报告

> **文档性质**:内部调研总结,供向上汇报使用。
> **调研对象**:AWS 开源项目 `dogwood-policy/dogwood`(治理语言)与
> `dogwood-policy/dogwood-local-engine`(本地授权引擎),以及本项目在其之上
> 搭建的 Python SDK / HTTP 适配 / 部署方案。
> **信息来源**:本仓库源码与文档、上游语言仓库源码、AWS Open Source Blog
> 《Introducing Dogwood: runtime verification for AI agents》。全部结论均有源码或原文出处,见文末附录。
> **一句话定位**:Dogwood 是一个**面向 AI Agent 工具调用边界的治理语言 + 授权引擎**,
> 在 Cedar 的"单点授权"之上补上"动作序列(历史/时序)"治理能力。

---

## 0. 给汇报对象的 TL;DR

| 维度 | 结论 |
|---|---|
| 它是什么 | 一门 **Agent 治理语言**(Cedar 派生 + 时间条件)加一个**有状态的本地授权引擎**。 |
| 解决的痛点 | Cedar 只能做"单点授权"(相同请求必然相同结果),**无法表达动作序列**;Agent 把多个动作编成工作流后,前置条件/频率/顺序都需要治理。 |
| 核心机制 | 引擎**持有事件流**:`request` 是决策点(回 allow/deny),`response` 是历史(只入账);**先落盘、再裁决**;按时间窗口**增量**维护状态;崩溃可恢复。 |
| AWS 的设计意图 | 把控制层放在**工具调用边界**。开源的引擎/服务端是**本地形态**(内嵌库 或 本机低权限进程);**规模化形态是托管的 Amazon Bedrock AgentCore Policy**,不在开源仓库里。 |
| 最强约束 | **单机、单写者**。一个 store 同一时刻只能被一个进程打开;没有集群/复制/发现/迁移机制;信任边界建立在"同机 + 不同 uid"之上。 |
| 我们的核心判断 | 它适合**单 Agent/单机/进程边界**的治理;要做**大规模、多租户、弹性多 pod**,开源引擎不是正确载体(要么用托管服务,要么把状态外置 + 无状态求值,即回到 OPA 架构)。 |

---

## 1. 背景与定位

### 1.1 AWS 为什么做 Dogwood

AWS 官方博客的出发点:

> Agent 之所以有用,是因为它能通过运行工具与外部世界交互;但这些**工具调用也正是最大的风险来源**。解决这一风险最可靠的方式,是在**工具调用边界**放一层控制。

据此,AWS **先前用 Cedar** 做了 **AgentCore Policy**(Amazon Bedrock AgentCore 里"每次工具调用决定是否放行"的那一层)。Cedar 的好处是快、可读、可自动推理,并且有一条审计与执行都依赖的保证:*相同请求产生相同决策*。

但 Cedar 的天然局限是**点态(point-in-time)**:每个请求独立求值,不依赖过去。于是当 Agent 把多个动作组合成长工作流时,**序列本身**就成了需要治理的对象——这正是 Dogwood 要补的缺口。

### 1.2 与 Cedar / OPA 的关系

| 维度 | Cedar | OPA/Rego | Dogwood |
|---|---|---|---|
| 决策模型 | 点态授权 | 点态、无状态纯函数 | **带历史的有状态流处理** |
| 能否表达动作序列 | 否 | 需自建(外部塞历史) | **是(核心能力)** |
| 状态 | 无 | 无(调用方提供 `input`/`data`) | 引擎自持事件历史 + 窗口状态 |
| 与 Cedar 兼容 | — | — | **任何合法 Cedar 都是合法 Dogwood**,无需迁移 |

Dogwood **构建在 Cedar 之上**(继承 `permit`/`forbid`、deny-by-default、forbid 覆盖 permit),再加一层来自 **MFOTL(度量一阶时序逻辑)** 的时间算子。

### 1.3 开源形态 vs 托管形态

| 形态 | 内容 | 定位 |
|---|---|---|
| **开源** | 语言 + parser/validator/**reference interpreter**(`dogwood-language`);本地引擎 `dogwood-local-engine`;本地服务端 demo `dogwood-server`;HTTP 适配器 `dogwood-http` | **本地 / 嵌入**用;参考实现,自述"非生产/demo" |
| **托管** | **Amazon Bedrock AgentCore Policy** 中的 Dogwood 支持 | 大规模、多租户的生产形态(不在本仓库) |

> 本仓库自述为 "a published, read-only mirror of an internal repository"——它是开放出来的参考实现,不是 AWS 的生产服务本身。

---

## 2. 核心设计

### 2.1 事件模型

每次工具调用产生两个事件:

- **`request` = 决策点**:引擎返回 allow/deny。
- **`response` = 历史**:只入账,无裁决。

> 关键术语:**decision kind**(决定是否有裁决)由所安装的 **event schema** 决定,默认是 `request`。切换 schema 可自定义哪些 kind 是决策点。**不要硬编码**——控制面 `status` 会报 `decision_kinds`。

每个事件携带两类字段,刻意分开:

- **`logged`**:给**未来**看的历史记录(未来时点用来关联的字段)。
- **`context`**:给**现在**看的请求字段。

策略里 `input.amount: context.input.amount` 读作:"历史那笔的 `input.amount` == 当前这笔的 `context.input.amount`",这就是**相关性匹配**。

### 2.2 一次 submit 的三步:线性化 → 落盘 → 求值

```
加锁(串行化) → 盖时间戳(严格单调) → append + fsync → 增量求值 → 返回裁决
```

要点:

- **时间戳由引擎在 append 时赋予**,客户端**不能**传 `ts`(传了会被拒)。append 顺序**就是**事件时序。
- **先落盘再裁决**:有裁决的事件一定已在磁盘上。
- 代价:每个事件一次 fsync(实测约 **1.18 ms**,单实例约 **845 events/s**;机器相关)。

### 2.3 增量求值与窗口有界

每个时间条件(`formerly within 1h …`、`count_within`、`sum_within` …)维护**只属于自己的窗口**,事件到达时增量更新,**不重扫全历史**。事件滑出窗口即被淘汰,因此:

> **窗口有界 ⇒ 状态有界。**

### 2.4 持久化与恢复

- 存储为嵌入式 **redb**(ACID、追加日志 + 快照)。
- 恢复 = 加载最新 **snapshot** + **重放**其后的日志记录。
- 崩溃语义:**已确认的提交不会丢**;无法恢复的 store **拒绝打开**(宁可不可用,也不带残缺历史服务)。

### 2.5 策略更新语义

- `install` 替换整个策略集(声明式);`batch` 施加一批动词(add/update/delete/set schema…)作为一个原子变更。
- 新装的策略**从空历史开始**,只判断其安装之后的事件;未被改动的策略**保留**已积累的历史。
- 策略更新本身也是日志里的一条记录,与事件共享同一顺序,**要么全生效,要么整批被拒且运行集不变**。

---

## 3. Data / Control 分离(本项目的核心安全设计)

### 3.1 两个 socket、两套互不相交的动词

| | **data plane**(面向被监控的 agent) | **control plane**(授权写策略) |
|---|---|---|
| 动词 | 只有 `Submit` + `Ping` | `Install` / `Batch` / `List` / `Status` / `GetPolicy` / `GetSchema` / `Checkpoint` |
| socket | Unix socket,`0666`(需能 connect) | Unix socket,`0700`,且位于 `0700` 私有目录 |
| 认证 | 无(靠动词集本身) | 内核 `SO_PEERCRED` 的 **uid 白名单** |
| 谁能用 | 被治理的 agent | 运维 |
| 能否改策略 | **不能** | 能 |

> 关键:**data socket 的 `DataRequest` 类型里根本不存在任何"读/改/停用策略"的变体**。这是**类型系统层面的结构性缺失**,不是运行时的一行 `if`——即使 data 路径上有 dispatch bug 也够不到 `apply`。

### 3.2 三层纵深防御

1. **文件权限**:control socket `0700`,嵌在 `0700` 目录中;agent 拿到路径也连不上。
2. **对等凭据白名单**:每个 control 连接的 uid 由内核 `SO_PEERCRED` 校验,客户端**无法伪造**(不像事件体里的 `principal` 是自称的)。默认白名单 = server 自己的 uid。
3. **类型级动词隔离**:即便前两层被绕过,data 路径也没有能改策略的东西。

### 3.3 为什么必须这么分

威胁模型是决定性的:**data socket 必须被"我们不信任的那个进程"(agent)访问**,因为它要提交事件、拿裁决。若 agent 触得到 control 动词,它就能删掉约束自己的 `forbid`、装上放行一切的 `permit`,治理当场失效。

> 一句话:你**不能用一个 agent 自己能控制的规则去治理 agent**。`dogwood-server` 的存在不是为性能,而是为**防篡改**。

### 3.4 fail-closed 与平台限制

- 这套边界默认 **fail-closed**:非 Linux 平台取不到 peer credential,control plane **拒绝所有连接**(宁可服务不可用,也不用"没有边界的可用性"顶替)。
- 因此有平台差异:macOS 上数据面可用、**控制面一律拒绝**;需"停服务→离线用库装策略→再起服务"。

---

## 4. SDK 语义:admin / decide / record

### 4.1 三者真实区别

| 方法 | 面 | 提交的事件 | 落盘? | 有裁决? | 进历史? |
|---|---|---|---|---|---|
| `decide` | data | `request`(decision kind) | **是** | 是 | **是** |
| `record` | data | `response`(history kind) | **是** | 否 | **是** |
| `admin.*` | control | 策略变更 | 是 | — | — |

> **易错点**:`decide` 和 `record` 的区别**不是**"求值 vs 写状态",而是"这张事件算不算决策点"。**两者都会落盘并进同一份历史**。

### 4.2 "只 decide 不 record" ≠ OPA

常见误解:以为 `decide` = 无状态求值、`record` = 记忆。实际:

- `decide` 追加的 `request` 事件**仍在累积历史**,因此 `count_within(...::request)`、`formerly ...::request` 这类规则照常有真实历史。
- 空掉的只是 **outcome(`response`)那一段**。

判断:

- 若策略只依赖当前请求(纯 Cedar 子集):**裁决数值**与无状态引擎一致;但 `decide` 仍是一次 fsync 的持久写 + 串行化,且 request 历史仍在累积,所以严格说是"OPA + 半条事件日志"。
- 若策略依赖 `::response`:只 decide 不 record → 这些窗口**恒空** → 表现为 **fail-closed 的持续 DENY**(看起来像"策略写错了",而非"历史漏了")。

> **结论:decide-only = "只见过前半段事件的时间引擎",不是 OPA。**

---

## 5. 记录保留与审计

### 5.1 两层"遗忘"

| 层 | 保留什么 | 何时遗忘 |
|---|---|---|
| **派生状态**(内存 + snapshot) | 窗口内的历史 | 事件滑出该策略的 `max_window` 即前台剪枝 |
| **原始日志**(redb) | 事件/策略变更记录 | 下一次 **checkpoint** 后被回收 |

细节:

- 每条时间条件的剪枝地平线 = 其窗口的**嵌套求和**;超过则从状态中删除。
- 剪枝是**事件驱动**:没有新事件就不会触发清理。
- checkpoint 落盘的是**派生状态**(不是原始事件),之后窗口外的原始事件**永久消失**。
- 服务端默认**每 10,000 个事件**自动 checkpoint,**正常关闭**也会 checkpoint。

### 5.2 保留多久

**有效保留 = 两层里较短的那个**:

- 对**裁决**的影响:到该策略的窗口为止;
- **原始事件**:到下一次 checkpoint 为止。

### 5.3 不是审计日志

- 引擎的日志会被 checkpoint **裁剪**,**不能当作审计日志**。
- snapshot 保存的是**派生状态**,不保存完整事件历史;**裁决结果本身也不落盘**。
- 需要审计/合规留痕 → **必须在 harness 侧另存一份**,配自己的保留策略。

---

## 6. 服务化与规模化

### 6.1 三种形态(本仓库)

1. **内嵌库 `dogwood-local-engine`**(推荐):在 harness 进程内 `open`/`install`/`submit`。
2. **本地守护进程 `dogwood-server`**:把策略集、监控器、日志放到 OS 进程边界之后,通过**两个 Unix socket** 提供数据面/控制面。
3. **HTTP 适配器 `dogwood-http`**:把两个 socket 翻成 HTTP + OpenAPI,用于拿不到 Unix socket 的环境;**不链接引擎、不打开 store**;自述**削弱**了安全(bearer token 替代内核凭据),默认只绑 loopback。

> 也有 socket + TCP 的部署变体(经转发器把引擎原协议放到 TCP 上);此时控制面端口必须收窄,因为转发器以自己的 uid 连引擎。

### 6.2 隔离模型:默认按 principal 分键

默认 **event schema 是 pinned 的**:在每种事件(kind)上声明

```
pin callerPrincipal: <principalType> = principal
```

带来两件事:

1. **语义隔离**:每个时间条件自动与"当前请求的 principal"关联,**其它 principal 的事件不可见**——即"历史按 principal 分键"。多用户共用一个引擎,裁决互不串扰。
2. **可分区**:该 pin 是 universal + symmetric,引擎**自动进入 partitioned 模式**,每个 principal 一个 monitor shard;冷 shard(最后事件早于 `now - 窗口`)会被回收。

可在 `status` 中确认:`partition key: callerPrincipal`(或 `none (single instance)`)。

> 注意:若使用 **unpinned** schema,则**不可分区**,必须单实例看全部事件。

### 6.3 session 不是自动隔离的

默认 schema 中 `sessionId` / `requestId` 只是**普通字段,不是 pin**。因此默认语义是 **per-principal,不是 per-session**:同一用户的会话 A 产生的历史会影响会话 B 的裁决。

需要 session 级隔离时:在策略中**显式按 session 关联**(`...::request{ sessionId: context.sessionId }`,并保证 logged/context 都带该字段),或自定义 schema 把 pin 换到 session 维度(pin 集只有一个,二选一会改变全局语义与分区键)。

### 6.4 容量与瓶颈

以"5000 DAU × N session"为例:

| 关注点 | 现状 |
|---|---|
| 写路径 | 每次 submit 一次 fsync + 一把全局写锁 → **单实例串行化** |
| 状态规模 | 分区模式下 ∝ "活跃 principal 数 × 窗口内事件数",冷 shard 自动回收;不随历史总量增长 |
| 吞吐量级 | fsync 地板 ≈ 1.18 ms/事件,单实例 ≈ 845 events/s(机器相关) |
| 峰值 | 平均值通常可承受;**峰值并发 + 单锁串行**是主要风险 |

> 诚实评估:5000 DAU 平均负载未必压垮单实例,分片更多是为了**峰值余量、故障隔离与内存**。

---

## 7. 多 pod / 弹性扩缩:核心风险与方案

### 7.1 会出什么问题

普通 Service(round-robin / least-conn)按请求分发,会让**同一个 principal 的事件落到不同 pod**,历史被劈开。失败方向**不对称**:

- `forbid ... when formerly ...`:历史缺失 → 条件不成立 → **放行了本该拒绝的动作(fail-open,危险方向)**。
- `permit ... when formerly ...`:历史缺失 → 拒了本该放行的 → fail-closed(可用性问题)。

> 即路由错乱不是"略有差异",而是**可能绕过 guardrail**。

### 7.2 扩容为何让问题更糟

- 一致性哈希能稳住分布,但**实例数一变**,约 `1/N` 的 principal 换主人;新主人 store 为空 → 这些 principal 的窗口**归零**,需新事件重新"热"起来。
- 若按负载自动扩缩,环会反复抖动 → **反复出现 fail-open 窗口期**。

### 7.3 用共享磁盘 / 同一文件解决?——不行

- redb 是嵌入式 **`ExclusiveWriter`**(默认**锁整个文件**);第二个进程打开会直接失败:`Database already open. Cannot acquire lock.`——不是变慢,是**起不来**。
- 引擎在进程生命周期内**常驻持有**该 DB,不是每次请求开关文件,无法"用外部锁轮流打开"。
- redb 虽有实验性的 `MultiWriter` 模式,但:(1) 依赖**字节范围文件锁**,只在本地文件系统可靠;(2) 引擎的 offset 缓存在**进程内存**,跨进程会错乱。故也不可行。
- **NFS/SMB 等网络文件系统**的 mmap 一致性、fsync、advisory lock 跨主机不可靠 → 有损坏风险。

> 唯一安全的是"**同一时刻只有一个 writer**"的**迁移**(fence 停写 → checkpoint → 拷贝文件 → 切换路由)——那是**搬迁**,不是**共享**。

### 7.4 用 Redis 当存储后端?——也不行

- redb 需要**字节可寻址的块设备**(read/write/set_len/sync);Redis 不是,模拟既慢又满足不了持久化假设。
- `open_with_backend` 是**私有**的,对外只有文件一条路,无法注入自定义后端。
- 本质问题:**monitor 的派生窗口状态、写锁、时钟都在进程内存**;Redis 当存储共享不了这些。

### 7.5 Redis 的两种正确用法

| 角色 | 做法 | 代价 |
|---|---|---|
| **A. 协调 / ownership 表**(推荐,互补) | 用 Redis(或 etcd/ZK)存 `principal → pod` 映射 + leader/lock;实例数变化时映射不乱跳,ownership 变更是**显式**的 | 权威窗口状态仍在 owner pod 本地;Redis 只做路由表与 fence |
| **B. 共享历史 + 无状态求值** | 每 principal 一个 Redis Stream;每次 decide 按窗口拉回事件 → 重建 trace → 用参考解释器求值 | 这就是 **OPA on Redis**:放弃增量求值、放弃引擎的时间戳/顺序保证、放弃 append-then-decide 原子性;窗口保留/裁剪/恢复全自己来 |

### 7.6 安全做法

1. **按 schema 的 partition key(默认 = principal)做粘性路由**;同一 principal 的**所有**事件必须落到同一实例。
2. **不要把有状态的 shard 集做成可弹性抖动**;留足余量,变更走受控 ownership。
3. **StatefulSet + 每 pod 独立 PVC + 稳定 pod 身份**,让重启复用自身状态。
4. **迁移必须受控**(fence + checkpoint + 拷贝 store + 切换),引擎不提供在线迁移。
5. **监控**:`status` 的 `partition key` 必须非空;某 pod 首次收到某 principal 事件 = "窗口冷启动"信号,应告警/核对,而非当正常流量。

---

## 8. 结论与建议

### 8.1 适用与不适用

| 场景 | 结论 |
|---|---|
| 单 Agent / 单机 / 需要进程防篡改边界 | **适合**:内嵌 `dogwood-local-engine`,或本机 `dogwood-server` |
| 需要"跨时间的工具调用治理"(前置条件/频率/顺序) | **适合**:这是它相对 Cedar/OPA 的独有价值 |
| 需要审计留痕 | **不适合直接依赖引擎**:自建审计 |
| 大规模 / 多租户 / 弹性多 pod 自建 | **不适合用开源引擎硬撑**:见第 7 节 |

### 8.2 三条路线

| 目标 | 载体 |
|---|---|
| 小规模 / 单机 / 进程边界 | 用 `dogwood-local-engine` 内嵌,或本机 `dogwood-server` |
| 大规模 / 多租户 / 弹性 | 用托管的 **Amazon Bedrock AgentCore Policy(Dogwood 支持已上线)** |
| 自建无粘性水平扩展 | 放弃单写者模型,历史交给 Redis + 无状态求值(= 回到 OPA 架构) |

### 8.3 风险清单(汇报要点)

1. **单写者**:一个 store 同一时刻仅一个进程;无集群/复制/迁移。
2. **历史劈开 = fail-open**:多 pod 路由不当会**放行**本该拒绝的动作。
3. **不是审计日志**:日志会被 checkpoint 裁剪;裁决不落盘。
4. **控制面平台限制**:peer-cred 仅 Linux;非 Linux 控制面 fail-closed。
5. **生态与成熟度**:上游为早期项目;reference interpreter / server 均标注非生产。
6. **语言限制**(继承自上游):时间子语言只有 `&&` / `!`(无 `||`);聚合只有 `count`/`sum`;窗口必须**有界**(默认上限 24h)。

### 8.4 一句话总结

> **Dogwood = "Agent 治理语言 + 本地参考引擎";它的规模化形态是 AWS 的托管服务。**
> 想在开源 repo 上做弹性多 pod,是在**逆着它的原始设计**走——
> 要么接受本地/单实例形态,要么外置状态走无状态求值,要么用托管服务承担规模化。

---

## 附录 A:关键文件索引(本仓库)

| 关注点 | 位置 |
|---|---|
| 引擎总览 / 三步流程 / 恢复 | `dogwood-local-engine/README.md` |
| 上游(AWS)README 原文留存 | `README.upstream.md` |
| 服务端为何存在(防篡改) | `dogwood-server/src/lib.rs` 模块文档 |
| 两个 socket 即边界 | `dogwood-server/src/server.rs` 模块文档 |
| 数据面/控制面动词集 | `dogwood-server/src/protocol.rs` |
| peer-credential 与 uid 白名单 | `dogwood-server/src/peer.rs` |
| 分区计划(ShardPlan) | `dogwood-local-engine/src/shard.rs` |
| 每 principal 一个 shard + 冷 shard 回收 | `dogwood-local-engine/src/partition.rs` |
| HTTP 适配器(控制面) | `dogwood-http/src/control.rs`、`dogwood-http/src/lib.rs` |
| SDK 语义 | `python-sdk/dogwood_sdk/client.py` |
| Agent 接入机制说明 | `python-demo/dogwood_middleware.py` 模块文档 |
| 9 个银行场景策略 | `python-demo/hsbc/policies/*.dw` |
| OPA 对比全文 | `docs/OPA-vs-Dogwood-全面对比与部署建议.md` |

## 附录 B:术语表

| 术语 | 含义 |
|---|---|
| **decision kind / `request`** | 决策点事件,引擎返回裁决 |
| **history kind / `response`** | 历史事件,只入账无裁决 |
| **`logged` vs `context`** | 前者是给未来的历史记录字段,后者是当前请求字段 |
| **pin** | event schema 里对某字段的强制绑定;universal + symmetric 时可用于分区 |
| **partition key** | 由 pin 推导出的分区键;默认 = `callerPrincipal` |
| **partitioned 模式** | 每 principal 一个 monitor shard 的运行模式 |
| **checkpoint / snapshot** | 把派生状态落盘并裁剪日志的操作 |
| **max_window** | 某时间条件所有窗口的嵌套求和,即剪枝地平线 |
| **data plane / control plane** | 提交事件的低权限面 / 改写策略的高权限面 |
| **fail-closed / fail-open** | 出错时"默认拒绝" / "默认放行";后者是危险方向 |

## 附录 C:信息来源

1. 本仓库源码与文档:`README.md`、`README.upstream.md`、`dogwood-local-engine/README.md`、
   `dogwood-server/src/{lib,server,protocol,peer,routing}.rs`、
   `dogwood-local-engine/src/{shard,partition,log,durable,incremental}.rs`、
   `dogwood-http/*`、`python-sdk/*`、`python-demo/*`、`docs/*`。
2. 上游语言仓库 `dogwood-policy/dogwood`:`README.md`、
   `configuration/event-schemas/pinned.dwschema`、`src/extension/temporal/*`、`src/interpreter/eval.rs`。
3. 依赖库 redb 4.3.0:`src/db.rs`(`ConcurrencyMode` 与文件锁语义)。
4. AWS Open Source Blog,*Introducing Dogwood: runtime verification for AI agents*:
   <https://aws.amazon.com/blogs/opensource/introducing-dogwood-runtime-verification-for-ai-agents/>
