# OPA(Rego) vs Dogwood：两个独立实现 + 一个对比器

两边**分别成目录、各自能单跑**，读起来互不干扰：

```text
opa-vs-dogwood/
├── scenario.py          # 两边共用的事件脚本（只有数据，没有引擎逻辑）
├── dogwood/             # ← Dogwood 侧：历史由引擎持有
│   ├── run_dogwood.py   #   起引擎、装策略、submit 事件、读裁决
│   ├── policy.dw        #   `formerly within 5s ...` 直接把时序写进策略
│   └── schema.cedarschema
├── opa/                 # ← OPA 侧：Rego 无状态，历史由脚本自己攒
│   ├── run_opa.py       #   维护 history、组装 input、调 opa eval
│   └── policy.rego      #   手写窗口过滤 + 同额匹配
├── compare.py           # 薄对比器：分别跑两边，再并排比较
└── README.md            # ← 你在这里
```

三种跑法（各自细节见子目录里的 README）：

```bash
python3 dogwood/run_dogwood.py     # 只看 Dogwood：DENY → ALLOW →（6 秒后）DENY
python3 opa/run_opa.py             # 只看 OPA（历史自己攒）
python3 compare.py                 # 并排比较 + 演示 OPA 的"调用方出错"
```

`--quick` 可跳过 6 秒等待（但那样"窗口过期"那一步不会真的过期）。

## 对比的规则

银行老例子：转账 ≤ 10000 直接放行；> 10000 必须有**同一金额、窗口内、已批准**的历史。

窗口故意取 `5s` 而不是 `1h`，是为了让"事件滑出窗口"能在几秒内演示出来 —— Dogwood 的
时间戳由 store 赋值，客户端无法伪造，所以没法"假装过了两小时"。
两边跑的**同一段事件脚本**在 `scenario.py` 里，`compare.py` 还会把 Dogwood 赋的时间戳
交给 OPA 侧复用，保证**时间轴不是变量**。

## 先装 OPA（单二进制，62 MB）

```bash
BASE=https://github.com/open-policy-agent/opa/releases/download/v1.21.1

curl -sSL -o /tmp/opa "$BASE/opa_darwin_arm64_static"
curl -sSL -o /tmp/opa.sha256 "$BASE/opa_darwin_arm64_static.sha256"
shasum -a 256 /tmp/opa          # 与 /tmp/opa.sha256 的内容比对

mkdir -p ~/.local/bin
chmod +x /tmp/opa && mv /tmp/opa ~/.local/bin/opa
~/.local/bin/opa version        # 期望 v1.21.1（或 brew install opa）
```

其他平台把 `darwin_arm64` 换成对应架构（`linux_amd64`、`linux_arm64`…）。

## OPA 是怎么做时序的：它不做，你来做

Rego **每次求值都是无状态的**：`input` 里有什么，它就只能看什么。所以窗口过滤、同额匹配、
"是否已批准"全部要在策略里手写，而 `input.history` 由**调用方组装**：

```rego
allow if {
    input.request.amount > 10000
    some approval in input.history          # ← 调用方传进来的历史
    approval.approved == true
    approval.amount == input.request.amount
    age := input.now - approval.ts          # ← 时间也是调用方给的
    age >= 0
    age <= window_ns
}
```

同一规则在 Dogwood 里（`dogwood/policy.dw`）：

```text
permit (principal, action == HSBC::Action::"transfer", resource)
when {
    context.input.amount > 10000
    && temporal {
        formerly within 5s HSBC::Action::"approve_transfer"::response{
            input.amount: context.input.amount,
            output.approved: true
        }
    }
};
```

`formerly within 5s` 由引擎维护：事件谁存、窗口怎么滑、过期怎么淘汰、重启怎么恢复，
都在引擎里。OPA 侧那几件事**一件都没省**，只是搬到了调用方（`opa/run_opa.py`
里那个 `history` 列表）。

## 实测结果

```text
【主对比】大额转账需要 5 秒内的同额批准
  1) transfer 20000（无批准）      Dogwood=DENY    OPA=DENY    ✅
  2) approve 20000（历史事件）       —               —
  3) transfer 20000（有批准）       Dogwood=ALLOW   OPA=ALLOW   ✅
  …等 6 秒，让批准滑出 5 秒窗口…
  4) transfer 20000（等 6 秒后）     Dogwood=DENY    OPA=DENY    ✅
```

**同一个时刻，只改调用方交上来的历史**（Dogwood 此刻 = ALLOW，它自己持有历史）：

| 交上来的历史（variant） | OPA 的裁决 | Dogwood 参考 | 说明 |
|---|---|---|---|
| `full` 完整 | ALLOW | ALLOW | ✅ 一致 |
| `dropped` 漏传那笔批准 | DENY | ALLOW | ❌ 结论反了，引擎无从纠正 |
| `stale` 时间戳前移 60s | DENY | ALLOW | ❌ 时间轴也是调用方给的 |

核心差别在这里：**OPA 的正确性跟着调用方走**；Dogwood 把"历史 + 时间"收进了引擎，
策略写出来就是可执行的语义。

## 一张表看完

| 维度 | OPA / Rego | Dogwood |
|---|---|---|
| 定位 | 通用策略引擎（任意 JSON 决策） | 时序授权引擎（看历史的裁决） |
| 状态 | **无状态**：每次只看 `input` | **有状态**：引擎持有事件历史 |
| 历史谁维护 | **调用方**（DB/Redis + 自己筛窗口） | 引擎（持久化、窗口、淘汰、恢复） |
| 策略里的时序 | 手写过滤/聚合/排序 | `formerly`/`previous`/`since` + `count`/`sum` |
| 时间 | 调用方提供 | store 赋值、严格递增、不可回填 |
| 恢复 | 你存什么就有什么 | 快照 + 重放，重启接着算 |
| 分片/键 | 自己按 key 查历史 | schema 声明 pin，引擎自动分区 |
| 审计 | **decision logs** 成熟，还有 `opa test/fmt/check` | 每条裁决带 `reason`，但**不落盘** |
| 生态与部署 | CNCF、K8s 生态、bundle 分发、Wasm、跨语言 | Rust 库 + JSON over Unix socket 的服务 |
| 延迟 | 无状态极快，但要付"取历史"的往返 | 每事件一次 fsync（≈1.2 ms 地板） |
| 适合 | 点状授权、统一策略分发、K8s/API | 顺序、限流、累计、"读过机密之后…" |

## 什么时候用哪个

- **只有点状判定**（当前请求是否合规）→ **OPA**。别为了时序把状态引进 OPA，那部分它不擅长。
- **需要看历史**（顺序、限流、累计、信息隔离）→ **Dogwood**（传统世界对应 Drools Fusion / Flink CEP）。
- **两个都要** → 组合：**OPA 做统一策略分发与点状授权，Dogwood 做时序裁决**，
  由 guard 服务串起来（例如先问 Dogwood 拿时序结论，把它当作一个事实喂给 OPA）。
  两者职责不重叠。

## 一句诚实的补充

这个 demo 不是说 OPA 差。OPA 的取舍是**用无状态换通用性**：它不替你管状态，所以能用在任何
语言、任何后端、任何数据形状上，还带成熟的 decision logs、bundle 灰度、Wasm 嵌入和
`opa test`。Dogwood 反过来：把状态收进引擎，换来"时序条件可以写进策略"，代价是生态小、
依赖 Rust、单机单写者。

选型问一句就够：**这份规则需要知道"之前发生过什么"吗？** 不需要 → OPA；需要 → Dogwood
（或 CEP 那一路）。
