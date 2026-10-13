# Dogwood 侧（独立实现）

**这一侧的核心**：历史由**引擎**持有，时序条件写在策略里。

```bash
python3 run_dogwood.py                      # 跑完 4 步（约 10 秒，含 6 秒窗口过期等待）
python3 run_dogwood.py --quick              # 跳过等待
python3 run_dogwood.py --json-out /tmp/dw.json   # 写出裁决 + 引擎赋的时间戳
```

文件：

| 文件 | 作用 |
|---|---|
| `policy.dw` | 策略：`formerly within 5s ...` 直接描述时序 |
| `schema.cedarschema` | 动作 schema（`transfer` / `approve_transfer`，含 `context.output`） |
| `run_dogwood.py` | 起引擎、装策略、submit 事件、打印裁决 |

前置：`cargo build --release -p dogwood-server`；装策略的小工具在 `~/dw-install`
（跑一次 `../../test_with_real_engine.sh` 会生成；也可用 `DW_HELPER_DIR` 指定）。

**这一侧没有的东西**：没有历史数组、没有窗口过滤代码、没有时间戳参数。
策略写出来什么样，引擎就按那个语义执行。
