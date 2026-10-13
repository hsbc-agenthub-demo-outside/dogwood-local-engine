"""薄对比器：分别跑两边（各自独立实现），再并排比较。

    python3 compare.py            # 跑两边 + 演示 OPA 的"调用方出错"
    python3 compare.py --quick    # 跳过 6 秒的窗口过期等待

它自己不实现任何一侧的逻辑，只做三件事：
1) 调 `dogwood/run_dogwood.py`（真引擎，历史在引擎里）；
2) 把引擎赋的时间戳交给 `opa/run_opa.py` 复用，让它跑同一段事件、同一时间轴；
3) 逐条对齐裁决，并额外跑 OPA 的 `dropped` / `stale` 两个变体。
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE / "dogwood"))
sys.path.insert(0, str(HERE / "opa"))

import run_dogwood  # noqa: E402
import run_opa      # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--quick", action="store_true", help="跳过窗口过期等待")
    args = parser.parse_args()

    print("=" * 72)
    print("① Dogwood 侧（历史在引擎里）")
    print("=" * 72)
    dw = run_dogwood.run(quick=args.quick)

    clock = Path(tempfile.mkdtemp(prefix="cmp-clock-")) / "dw.json"
    clock.write_text(json.dumps(dw, ensure_ascii=False))

    print()
    print("=" * 72)
    print("② OPA 侧（历史由调用方攒；时间戳复用上一步的引擎时钟）")
    print("=" * 72)
    opa_full = run_opa.run(quick=args.quick, variant="full", clock_file=str(clock))

    print()
    print("=" * 72)
    print("③ 并排比较（同一段事件、同一时间轴）")
    print("=" * 72)
    print(f"  {'步骤':<30} {'Dogwood':<10} {'OPA':<10} 一致?")
    mismatches = 0
    for a, b in zip(dw, opa_full):
        if a["kind"] == "response":
            continue
        same = a["allow"] == b["allow"]
        mismatches += 0 if same else 1
        print(f"  {a['label']:<30} {('ALLOW' if a['allow'] else 'DENY'):<10} "
              f"{('ALLOW' if b['allow'] else 'DENY'):<10} {'✅' if same else '❌'}")

    print()
    print("=" * 72)
    print("④ 只有 OPA 会遇到的失败：同一个时刻，只改'交上去的历史'")
    print("=" * 72)
    print("（下面两次变体只打结论，不打过程）")
    dropped = run_opa.run(quick=True, variant="dropped", clock_file=str(clock), quiet=True)
    stale = run_opa.run(quick=True, variant="stale", clock_file=str(clock), quiet=True)

    dw_allow = {a["label"]: a["allow"] for a in dw}
    print(f"\n  {'交上去的历史':<12} {'OPA':<8} {'Dogwood 参考':<14} 说明")
    notes = {"full": "一致", "dropped": "结论反了，引擎无从纠正",
             "stale": "时间轴也是调用方给的"}
    for variant, rows in (("full", opa_full), ("dropped", dropped), ("stale", stale)):
        for row in rows:
            if row["kind"] == "response" or "有批准" not in row["label"]:
                continue                      # 只挑"此刻本应 ALLOW"的那一步对比
            print(f"  {variant:<12} {('ALLOW' if row['allow'] else 'DENY'):<8} "
                  f"{('ALLOW' if dw_allow[row['label']] else 'DENY'):<14} {notes[variant]}")

    print()
    if mismatches:
        print(f"主对比有 {mismatches} 步不一致 —— 那才需要深挖。")
        return 1
    print("主对比全部一致：同样的策略、同样的事件、同样的时钟 → 同样的裁决。")
    print("差别在'历史归谁'：Dogwood 由引擎持有；OPA 由调用方提供，错了它也不知道。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
