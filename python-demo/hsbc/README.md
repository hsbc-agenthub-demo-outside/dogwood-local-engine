# HSBC 场景：9 条可单独维护的策略

每个场景是**一个独立的 `.dw` 文件 + 一段事件轨迹 + 逐条断言**，全部用**真引擎**
（Rust 的 `dogwood-server`）跑，不是替身。目的是让你能一条一条地改、一条一条地
验证，不用一次面对整套策略。

```bash
cd python-demo/hsbc
python3 run_hsbc.py --list        # 看有哪些场景
python3 run_hsbc.py 01            # 只跑一条（改完只重跑这条）
python3 run_hsbc.py 02 05 09      # 跑几条
python3 run_hsbc.py --all         # 跑全部
python3 run_hsbc.py 01 --keep     # 保留 store 目录，方便手工接着玩
```

每个场景用**全新的 store**，所以结论互不污染。前提：`target/release/dogwood-server`
已构建，且装策略的小工具在 `~/dw-install`（跑一次 `../test_with_real_engine.sh`
就会生成；也可用 `DW_HELPER_DIR` 指到别处）。

```text
hsbc/
  schema.cedarschema      # 银行动作的 action schema（含 context.output）
  policies/*.dw           # 9 条策略，每条自包含
  run_hsbc.py             # 装策略 → 起服务 → 重放轨迹 → 断言
```

## 九个场景

| # | 规则 | 用到的时间机制 | 看点 |
|---|---|---|---|
| 01 | 大额转账必须有同额批准（maker-checker） | `formerly within 1h` | 第 2 笔拒、第 4 笔放行；同一份批准能被反复用 |
| 02 | 一小时内最多 5 笔转账 | `count_within` + `::request` | 第 6 笔拒；request 口径让并发也绕不过 |
| 03 | 一小时累计 <= 50000 | `sum_within` | 第 3 笔把累计推到 60000，被拒 |
| 04 | 单笔不得超过已结算总额（反异常） | `bind` + `sum_within` + `::response` | 用"已结算"而不是"已申请"口径 |
| 05 | 读过机密客户数据后 8 小时内禁止对外转账 | `formerly` + 输入字段过滤 | 对内转账不受影响 |
| 06 | 一小时最多 3 个不同收款人 | `count_distinct_within` | 第 4 个拒；转回老收款人也拒 |
| 07 | 制裁名单硬禁（纯 Cedar） | 无（`forbid` 覆盖 `permit`） | 放行一切 + 名单 forbid，名单赢 |
| 08 | 会话有效性：`!失败 since 成功` | `since` + 否定左操作数 | 中间来一次验证失败，会话即失效 |
| 09 | 一次批准只能消费一次 | 聚合对聚合（count vs count） | 转账数 > 批准数就拒 |

## 六个必须知道的语义（全是跑真引擎逼出来的）

1. **deny by default。** 没有 permit 覆盖就是拒。所以每条"只有 forbid"的策略文件里
   我都放了一条兜底 `permit`，否则你会看到"全都拒绝"且没有任何报错。反过来，
   场景 01/09 故意**不带**兜底 permit —— 它们的 permit 就是全部授权来源。
2. **历史按 principal 分键。** 默认事件 schema 有 `pin callerPrincipal = principal`，
   所以**每条事件（包括 response）都要带 `principal`**。漏掉它，这笔历史会落在别的
   键上，之后的请求永远匹配不到 —— 症状同样是"静默 DENY"。
3. **裁决结果不落盘。** 策略看不到"上一笔是被允许还是被拒绝"；**被拒的 request
   一样进日志、一样被统计**（场景 09 就踩到了这点）。方向是安全的（重试消耗额度），
   但你必须知道。
4. **时序子语言没有 `||`。** 只有 `&&` 和 `!`；"或者"要用**多条规则**表达
   （场景 09 的"小额 or 有批准"就是两条 permit）。
5. **聚合只能当比较操作数**，且窗口必须有界：单位只有 `s/m/h/d`，默认上限 24h，
   所以"从此永远"只能用足够长的窗口近似（场景 05）。要放宽得上事件 schema 的
   `max_window`。
6. **`::request` 与 `::response` 是两个口径。** request 包含"正在申请的这笔"，
   防并发绕过（02/03）；response 只包含已完成/已结算的，用来和历史实际体量比较（04）。

## 单独维护与上线

- 每个 `.dw` 文件自包含，改一条只重跑一条：`python3 run_hsbc.py 03`。
- 生产里**不要用 `install` 更新策略**：它是 reborn-all 语义，会清空所有历史。
  保留历史要用 `batch` 的增量动词（`add`/`update`/`reset`），注意 `update` 也是
  重置语义（同名策略换内容 = 历史清零）。
- 场景之间是独立的；真要合成一套，把文件拼起来即可（策略集就是多条策略放在一起）。

## 练习

- 把场景 09 改成**只统计大额**（提示：宏做不到，要写原始聚合
  `count for (a: Long), (t: Timepoint). where (... && a > 10000 && tp(t))`，
  并给当前请求加 `when { context.input.amount > 10000 }`）。
- 用 `previous within 1h` 写一条"必须紧接在某事件之后"的规则（注意它只看**前一个**
  时间点，且第一个时间点恒为 false）。
- 把阈值从字面量改成从上下文读（`context.input.limit`），让同一条策略服务多个客户档位。
- 试试把某个 `1h` 改成 `25h` —— 你会被校验器拒绝，因为默认窗口上限是 24h。
