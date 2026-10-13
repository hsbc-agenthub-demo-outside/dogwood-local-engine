"""OPA 侧：Rego **无状态**，历史由**本脚本**维护并随每次求值传进去。

    python3 run_opa.py                       # 用本机时钟，自己攒历史（跑完整脚本）
    python3 run_opa.py --quick               # 跳过窗口过期等待
    python3 run_opa.py --clock-file /tmp/dw.json   # 复用 Dogwood 的时间戳（公平对比用）
    python3 run_opa.py --variant dropped      # 演示"调用方漏传历史"会怎样
    python3 run_opa.py --variant stale        # 演示"调用方给的时间戳不准"会怎样

这个文件里**没有 Dogwood 的影子**：它只做三件事 ——
（1）自己保存事件历史；（2）按窗口组装 input；（3）调 `opa eval` 求值。
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
OPA = Path(os.environ.get("OPA_BIN", Path.home() / ".local" / "bin" / "opa"))

sys.path.insert(0, str(ROOT))
from scenario import STEPS, transfer_fields  # noqa: E402

#: 与 policy.rego 里的 window_ns 一致（5 秒）
WINDOW_NS = 5 * 1_000_000_000


def decide(request: dict, history: list[dict], now_ns: int) -> dict:
    """调 `opa eval` 求值。历史和时间都由调用方（也就是本函数的上层）提供。"""
    payload = json.dumps({"request": request, "history": history, "now": now_ns})
    proc = subprocess.run(
        [str(OPA), "eval", "-f", "json", "-d", str(HERE / "policy.rego"), "-I",
         "data.dogwood_compare.decision"],
        input=payload, capture_output=True, text=True,
    )
    if proc.returncode != 0:
        raise SystemExit(f"opa eval 失败：{proc.stderr.strip()}")
    value = json.loads(proc.stdout)["result"][0]["expressions"][0]["value"]
    return {"allow": bool(value.get("allow")), "reason": sorted(value.get("reason") or [])}


def _history_for(variant: str, history: list[dict], now_ns: int) -> list[dict]:
    """按变体决定"交给 OPA 的历史长什么样" —— 这一步是 OPA 特有的失败面。"""
    if variant == "full":
        return list(history)
    if variant == "dropped":          # 漏传那条批准（比如查询窗口边界写错了）
        return [h for h in history if h["kind"] != "approve_response"]
    if variant == "stale":            # 时间戳整体往前挪 60 秒（比如时钟/时区处理出错）
        return [dict(h, ts=h["ts"] - 60 * 10**9) for h in history]
    raise SystemExit(f"未知 variant: {variant}")


def run(quick: bool = False, variant: str = "full",
        clock_file: str | None = None, quiet: bool = False) -> list[dict]:
    """执行事件脚本。`clock_file` 给定时复用那边的时间戳（保证两边同一时间轴）。"""
    if not OPA.exists():
        raise SystemExit(f"找不到 OPA：{OPA}（装法见 ../README.md）")

    def say(*args) -> None:
        if not quiet:
            print(*args)

    fixed_clock: list[int] | None = None
    if clock_file:
        fixed_clock = [entry["ts"] for entry in json.loads(Path(clock_file).read_text())]
        say(f"OPA 侧：复用 {clock_file} 的时间戳（与 Dogwood 同一时间轴）")
    else:
        say("OPA 侧：使用本机时钟")
    say(f"        variant = {variant}")

    history: list[dict] = []
    results: list[dict] = []
    for i, step in enumerate(STEPS):
        if step.get("wait_seconds"):
            if fixed_clock is not None:
                pass          # 时间戳来自 Dogwood，间隔已经体现在数字里，不用真等
            elif quick:
                say(f"  （--quick：跳过 {step['wait_seconds']} 秒等待，"
                    "窗口不会过期，这一步会 ALLOW）")
            else:
                say(f"  …等 {step['wait_seconds']} 秒，让批准滑出 5 秒窗口")
                time.sleep(step["wait_seconds"])

        now_ns = fixed_clock[i] if fixed_clock else time.time_ns()
        request = transfer_fields(step["amount"])
        supplied = _history_for(variant, history, now_ns)
        verdict = decide(request, supplied, now_ns)

        # 关键：无论 OPA 怎么答，**这一笔都要由我们自己记进历史**，供下次求值使用。
        history.append({
            "kind": "approve_response" if step["action"] == "approve_transfer" else "transfer_request",
            "amount": step["amount"],
            "approved": bool(step.get("approved")),
            "ts": now_ns,
        })

        allow = verdict["allow"]
        if step["kind"] == "response":
            say(f"  {step['label']:<30} -> recorded (ts={now_ns})")
        else:
            say(f"  {step['label']:<30} -> {'ALLOW' if allow else 'DENY '} "
                f"(ts={now_ns}, reason={verdict['reason']})")
        results.append({"label": step["label"], "allow": allow, "ts": now_ns,
                        "kind": step["kind"], "amount": step["amount"],
                        "variant": variant, "history_size": len(supplied)})
    return results


def main() -> int:
    parser = argparse.ArgumentParser(description="用 OPA/Rego 跑同一段事件脚本（历史自己攒）")
    parser.add_argument("--quick", action="store_true", help="跳过窗口过期等待")
    parser.add_argument("--variant", default="full", choices=["full", "dropped", "stale"],
                        help="交给 OPA 的历史：完整 / 漏传批准 / 时间戳错位")
    parser.add_argument("--clock-file", help="复用 Dogwood 写出的结果 JSON 里的时间戳")
    parser.add_argument("--json-out", help="把结果写成 JSON")
    args = parser.parse_args()

    results = run(quick=args.quick, variant=args.variant, clock_file=args.clock_file)
    if args.json_out:
        Path(args.json_out).write_text(json.dumps(results, ensure_ascii=False, indent=2))
        print(f"  结果写入 {args.json_out}")
    print("\n注：Rego 每次求值只看 input —— 历史与时间都由本脚本提供。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
