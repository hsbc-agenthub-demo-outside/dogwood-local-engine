# OPA / Rego 侧（独立实现）

**这一侧的核心**：Rego **无状态**——每次求值只看 `input`。
所以历史必须由调用方（`run_opa.py`）自己保存、自己按窗口筛、自己组装进 `input`。

```bash
python3 run_opa.py                              # 用本机时钟，自己攒历史（跑完 4 步）
python3 run_opa.py --quick                      # 跳过等待
python3 run_opa.py --clock-file /tmp/dw.json    # 复用 Dogwood 的时间戳（公平对比）
python3 run_opa.py --variant dropped            # 漏传那笔批准 → 看它怎么被带偏
python3 run_opa.py --variant stale              # 时间戳整体前移 60s → 同样被带偏
```

文件：

| 文件 | 作用 |
|---|---|
| `policy.rego` | 规则：**手写**窗口过滤 + 同额匹配 + `input.history` 遍历 |
| `run_opa.py` | 自己维护历史、组装 `input`、调 `opa eval`、记录裁决 |

前置：OPA 二进制（装法见 [`../README.md`](../README.md)），默认在 `~/.local/bin/opa`，
也可以用 `OPA_BIN` 指定。

**这一侧没有的东西**：没有持久化、没有窗口淘汰、没有重启恢复。历史是这个 Python
进程内存里的一个 list —— 进程一退，历史就没了。这不是缺陷，是 Rego 的取舍：
它把状态留给你，换来通用性。

## 三个 variant 在演示什么

| variant | 交给 OPA 的历史 | 现象 |
|---|---|---|
| `full` | 完整 | 与 Dogwood 一致 |
| `dropped` | 少了那条批准 | **结论反了**（本该 ALLOW 却 DENY），而 OPA 无从纠正 |
| `stale` | 时间戳整体前移 60s | 窗口判断跟着错，同样偏离 |

这三种"调用方出错"在 Dogwood 侧不存在——那边没有"交历史"这个动作。
