"""Dogwood 侧：把事件 submit 给**真引擎**，由引擎维护历史。

    python3 run_dogwood.py            # 跑完整脚本（含 6 秒等待），打印裁决
    python3 run_dogwood.py --quick    # 跳过等待
    python3 run_dogwood.py --json-out /tmp/dw.json   # 顺带写出裁决与时间戳

这个文件里**没有 OPA 的影子**：它只管把事件喂给引擎、读回裁决。
时序条件（`formerly within 5s`）由 `policy.dw` 描述，历史由引擎保存。
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent                       # opa-vs-dogwood/
PYDEMO = ROOT.parent                     # python-demo/
REPO = PYDEMO.parent                     # 仓库根
SERVER = REPO / "target" / "release" / "dogwood-server"
HELPER = Path(os.environ.get("DW_HELPER_DIR", Path.home() / "dw-install"))

sys.path.insert(0, str(PYDEMO))          # dogwood_client 在 python-demo/
sys.path.insert(0, str(ROOT))
from dogwood_client import DogwoodClient                     # noqa: E402
from scenario import PRINCIPAL, RESOURCE, STEPS, transfer_fields  # noqa: E402


def _install(store: Path) -> None:
    proc = subprocess.run(
        ["cargo", "run", "--quiet", "--manifest-path", str(HELPER / "Cargo.toml"), "--",
         str(store), str(HERE / "policy.dw"), str(HERE / "schema.cedarschema")],
        capture_output=True, text=True, cwd=REPO,
    )
    if proc.returncode != 0:
        sys.stderr.write(proc.stdout + proc.stderr)
        raise SystemExit("真引擎拒绝了这份策略（上面的报错就是原因）")
    line = [l for l in (proc.stdout + proc.stderr).splitlines() if "安装" in l]
    print("  " + (line[-1].strip() if line else "策略已安装"))


def _start(store: Path) -> subprocess.Popen:
    log = open(store / "server.log", "w")
    proc = subprocess.Popen([str(SERVER), "--dir", str(store), "run"], stdout=log, stderr=log)
    for _ in range(150):
        if subprocess.run([str(SERVER), "--dir", str(store), "ping"],
                          capture_output=True).returncode == 0:
            return proc
        time.sleep(0.2)
    proc.kill()
    raise SystemExit("服务没起来，看 store/server.log")


def run(quick: bool = False, keep: bool = False) -> list[dict]:
    """执行事件脚本，返回 [{label, allow, ts}]（ts 是引擎赋的时间戳）。"""
    if not SERVER.exists():
        raise SystemExit(f"找不到 {SERVER}，先 cargo build --release -p dogwood-server")

    store = Path(tempfile.mkdtemp(prefix="dw-side-"))
    print(f"Dogwood 侧：store={store}")
    _install(store)
    server = _start(store)
    results: list[dict] = []
    try:
        with DogwoodClient(str(store / "run" / "dogwood.sock")) as client:
            for step in STEPS:
                if step.get("wait_seconds"):
                    if quick:
                        print(f"  （--quick：跳过 {step['wait_seconds']} 秒等待，"
                              "窗口不会过期，这一步会 ALLOW）")
                    else:
                        print(f"  …等 {step['wait_seconds']} 秒，让批准滑出 5 秒窗口")
                        time.sleep(step["wait_seconds"])

                if step["action"] == "approve_transfer":
                    logged: dict = {"input": {"amount": step["amount"], "approver": "bob"},
                                    "output": {"approved": True}}
                else:
                    logged = {"input": transfer_fields(step["amount"])}

                action = ('HSBC::Action::transfer' if step["action"] == "transfer"
                          else 'HSBC::Action::approve_transfer')
                resp = client.submit(action, step["kind"],
                                     principal=PRINCIPAL, resource=RESOURCE,
                                     logged=logged, context={"input": logged["input"]})
                allow = resp.get("status") == "decision" and resp.get("allowed") is True
                ts = int(resp["recorded_at_nanos"])
                if step["kind"] == "response":
                    print(f"  {step['label']:<30} -> recorded (ts={ts})")
                else:
                    print(f"  {step['label']:<30} -> {'ALLOW' if allow else 'DENY '} (ts={ts})")
                results.append({"label": step["label"], "allow": allow, "ts": ts,
                                "kind": step["kind"],
                                "amount": step["amount"]})
    finally:
        server.terminate()
        server.wait(timeout=10)
        if keep:
            print(f"  （store 保留在 {store}）")
    return results


def main() -> int:
    parser = argparse.ArgumentParser(description="把事件脚本喂给真 Dogwood 引擎")
    parser.add_argument("--quick", action="store_true", help="跳过窗口过期等待")
    parser.add_argument("--keep", action="store_true", help="保留 store 目录")
    parser.add_argument("--json-out", help="把结果写成 JSON（含引擎赋的时间戳）")
    args = parser.parse_args()

    results = run(quick=args.quick, keep=args.keep)
    if args.json_out:
        Path(args.json_out).write_text(json.dumps(results, ensure_ascii=False, indent=2))
        print(f"  结果写入 {args.json_out}")
    print("\n注：历史完全由引擎持有 —— 本脚本只 submit 事件、读回裁决。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
