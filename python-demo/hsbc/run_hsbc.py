"""跑 HSBC 场景：**装某一条策略 → 重放一段事件轨迹 → 逐条断言裁决**。

用法（在 python-demo/hsbc 下）：

    python3 run_hsbc.py --list            # 看有哪些场景
    python3 run_hsbc.py 01                # 只跑场景 1
    python3 run_hsbc.py 02 05 09          # 跑多个
    python3 run_hsbc.py --all             # 跑全部
    python3 run_hsbc.py 01 --keep         # 保留 store 目录，方便自己翻/手工接着玩

每个场景用**全新的 store**，所以结论互不污染；策略文件之间也完全独立，方便你
单独改一个、只重跑那一个。

前提：得有一个"装策略"的小工具（macOS 上控制面不可用，只能用库离线装）。
它在 python-demo/test_with_real_engine.sh 第一次运行时生成到 ~/dw-install；
也可以用 DW_HELPER_DIR 指到别处。
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent.parent
SERVER = REPO / "target" / "release" / "dogwood-server"
HELPER = Path(os.environ.get("DW_HELPER_DIR", Path.home() / "dw-install"))
POLICIES = HERE / "policies"
SCHEMA = HERE / "schema.cedarschema"

# dogwood_client 在上一级（python-demo/），它是协议客户端，跟场景无关，直接复用。
sys.path.insert(0, str(HERE.parent))
from dogwood_client import DogwoodClient  # noqa: E402

PRINCIPAL = 'HSBC::User::"alice"'
RESOURCE = 'HSBC::Account::"main"'

ACTION_TRANSFER = 'HSBC::Action::transfer'
ACTION_APPROVE = 'HSBC::Action::approve_transfer'
ACTION_READ = 'HSBC::Action::read_customer'
ACTION_VERIFY = 'HSBC::Action::verify_customer'


# ── 事件构造器 ─────────────────────────────────────────────────────────────
# request = 决策点（会拿裁决）；response = 历史（只入账）。两者都要带 principal，
# 因为默认事件 schema 用 `pin callerPrincipal = principal` 把历史**按主体分键**：
# 漏掉它，这笔历史就落在别的键上，之后的请求永远匹配不到。


def transfer(amount: int, expect: bool, payee: str = "ACME-001", payee_type: str = "internal") -> dict:
    fields = {"amount": amount, "payee": payee, "payee_type": payee_type}
    return {"action": ACTION_TRANSFER, "kind": "request", "input": fields, "expect": expect}


def transfer_settled(amount: int, payee: str = "ACME-001", payee_type: str = "internal") -> dict:
    """一笔**已结算**的转账（response 事件），供 04 那种"和已发生体量比"的规则用。"""
    return {
        "action": ACTION_TRANSFER,
        "kind": "response",
        "input": {"amount": amount, "payee": payee, "payee_type": payee_type},
        "output": {"reference": "TR-SETTLED"},
        "expect": "recorded",
    }


def approve(amount: int, approved: bool = True, approver: str = "bob") -> dict:
    return {
        "action": ACTION_APPROVE,
        "kind": "response",
        "input": {"amount": amount, "approver": approver},
        "output": {"approved": approved},
        "expect": "recorded",
    }


def read_customer(classification: str = "confidential") -> dict:
    return {
        "action": ACTION_READ,
        "kind": "response",
        "input": {"customer_id": "C-1001"},
        "output": {"classification": classification},
        "expect": "recorded",
    }


def read_request(expect: bool) -> dict:
    return {
        "action": ACTION_READ,
        "kind": "request",
        "input": {"customer_id": "C-1001"},
        "expect": expect,
    }


def verify(verified: bool) -> dict:
    return {
        "action": ACTION_VERIFY,
        "kind": "response",
        "input": {"customer_id": "C-1001"},
        "output": {"verified": verified},
        "expect": "recorded",
    }


# ── 场景 ───────────────────────────────────────────────────────────────────

SCENARIOS: dict[str, dict] = {
    "01": {
        "file": "01_dual_approval.dw",
        "title": "双人复核：大额必须有同额批准",
        "watch": "第 2 步拒、第 4 步放行；第 5 步说明'一次批准能被反复用'（要一次一用见场景 09）",
        "steps": [
            ("小额 5000，无需批准", transfer(5000, expect=True)),
            ("大额 20000，无批准", transfer(20000, expect=False)),
            ("审批 20000（只记历史）", approve(20000)),
            ("再转 20000", transfer(20000, expect=True)),
            ("又转 20000（同一份批准）", transfer(20000, expect=True)),
        ],
    },
    "02": {
        "file": "02_rate_limit.dw",
        "title": "频率限制：一小时最多 5 笔",
        "watch": "第 6 笔被拒；`::request` 让「正在申请的这笔」也计入，并发也绕不过",
        "steps": [(f"第 {i} 笔 1000", transfer(1000, expect=i <= 5)) for i in range(1, 7)],
    },
    "03": {
        "file": "03_cumulative_cap.dw",
        "title": "累计额度：一小时 ≤ 50000",
        "watch": "第 3 笔让累计到 60000，被拒",
        "steps": [
            ("转 20000（累计 20000）", transfer(20000, expect=True)),
            ("转 20000（累计 40000）", transfer(20000, expect=True)),
            ("转 20000（累计 60000）", transfer(20000, expect=False)),
        ],
    },
    "04": {
        "file": "04_anti_spike.dw",
        "title": "反异常：单笔不得超过已结算总额",
        "watch": "先结算 1000；500 放行、2000 被拒（超出已结算体量）、800 放行",
        "steps": [
            ("已结算 1000", transfer_settled(1000)),
            ("转 500（≤ 已结算 1000）", transfer(500, expect=True)),
            ("转 2000（> 已结算 1000）", transfer(2000, expect=False)),
            ("转 800（≤ 已结算 1000）", transfer(800, expect=True)),
        ],
    },
    "05": {
        "file": "05_pii_external_block.dw",
        "title": "信息隔离：读机密后 8h 内禁止对外转账",
        "watch": "读机密前的对外转账放行；读完之后对外被拒，**对内仍放行**",
        "steps": [
            ("对外转 100（还没读机密）", transfer(100, expect=True, payee_type="external")),
            ("读取客户资料（机密）", read_customer("confidential")),
            ("对外转 100（读过机密）", transfer(100, expect=False, payee_type="external")),
            ("对内转 100（不受影响）", transfer(100, expect=True, payee_type="internal")),
        ],
    },
    "06": {
        "file": "06_distinct_payees.dw",
        "title": "分散度：一小时最多 3 个不同收款人",
        "watch": "第 4 个收款人被拒；再转回 bob 仍被拒（窗口里已经有 3 个不同的）",
        "steps": [
            ("转给 bob", transfer(10, expect=True, payee="bob")),
            ("转给 carol", transfer(10, expect=True, payee="carol")),
            ("转给 dave", transfer(10, expect=True, payee="dave")),
            ("转给 erin（第 4 个）", transfer(10, expect=False, payee="erin")),
            ("转回 bob（仍算第 4 个）", transfer(10, expect=False, payee="bob")),
        ],
    },
    "07": {
        "file": "07_sanctions_forbid.dw",
        "title": "硬禁名单：forbid 覆盖 permit",
        "watch": "有一条「放行一切」的 permit，制裁名单依然被拒",
        "steps": [
            ("转给普通收款人", transfer(100, expect=True, payee="ACME-001")),
            ("转给 SANCTIONED-001", transfer(100, expect=False, payee="SANCTIONED-001")),
        ],
    },
    "08": {
        "file": "08_verified_session.dw",
        "title": "会话有效性：`!失败 since 成功`",
        "watch": "没验证过 → 拒；验证成功 → 放行；中间来一次验证失败 → 又拒",
        "steps": [
            ("未验证就读客户", read_request(expect=False)),
            ("验证成功", verify(True)),
            ("再读客户", read_request(expect=True)),
            ("验证失败一次", verify(False)),
            ("再读客户（会话已失效）", read_request(expect=False)),
        ],
    },
    "09": {
        "file": "09_consume_approval.dw",
        "title": "进阶：一次批准只能用一次（聚合对聚合）",
        "watch": "批准 20000 后第 1 笔放行、第 2 笔被拒（转账数 > 批准数）；被拒的尝试也计入",
        "steps": [
            ("审批 20000", approve(20000)),
            ("转 20000（消费这次批准）", transfer(20000, expect=True)),
            ("再转 20000（批准已用完）", transfer(20000, expect=False)),
        ],
    },
}


# ── 执行 ───────────────────────────────────────────────────────────────────


def install_policy(store: Path, policy: Path) -> None:
    proc = subprocess.run(
        ["cargo", "run", "--quiet", "--manifest-path", str(HELPER / "Cargo.toml"),
         "--", str(store), str(policy), str(SCHEMA)],
        capture_output=True, text=True, cwd=REPO,
    )
    if proc.returncode != 0:
        sys.stderr.write((proc.stderr or "").strip() + "\n")
        raise SystemExit(f"装策略被拒绝：{policy.name}（真引擎的报错见上）")


def start_server(store: Path) -> subprocess.Popen:
    log = open(store / "server.log", "w")
    proc = subprocess.Popen([str(SERVER), "--dir", str(store), "run"], stdout=log, stderr=log)
    for _ in range(150):
        if subprocess.run([str(SERVER), "--dir", str(store), "ping"],
                          capture_output=True).returncode == 0:
            return proc
        if proc.poll() is not None:
            raise SystemExit("服务启动即退出，看 store/server.log")
        time.sleep(0.2)
    proc.kill()
    raise SystemExit("服务 30 秒内没起来，看 store/server.log")


def run_step(client, step: dict) -> tuple[bool, str]:
    logged = {"input": step.get("input", {})}
    if "output" in step:
        logged["output"] = step["output"]
    response = client.submit(
        step["action"], step["kind"],
        principal=PRINCIPAL, resource=RESOURCE,
        logged=logged, context={"input": step.get("input", {})},
    )
    if step["expect"] == "recorded":
        ok = response.get("status") == "recorded"
        return ok, str(response.get("status"))
    allowed = response.get("status") == "decision" and response.get("allowed") is True
    return allowed == step["expect"], "ALLOW" if allowed else "DENY"


def run_scenario(key: str, keep: bool) -> bool:
    scenario = SCENARIOS[key]
    policy = POLICIES / scenario["file"]
    store = Path(tempfile.mkdtemp(prefix=f"hsbc-{key}-"))

    print(f"\n{'=' * 78}")
    print(f"场景 {key}：{scenario['title']}     [{scenario['file']}]")
    print(f"看点：{scenario['watch']}")
    print(f"{'=' * 78}")
    install_policy(store, policy)
    print("策略已被真引擎接受（解析 + 类型检查通过）")

    server = start_server(store)
    failures = 0
    try:
        with DogwoodClient(str(store / "run" / "dogwood.sock")) as client:
            for i, (label, step) in enumerate(scenario["steps"], start=1):
                ok, got = run_step(client, step)
                want = "ALLOW" if step["expect"] is True else ("DENY" if step["expect"] is False else "recorded")
                mark = "✓" if ok else "✗"
                if not ok:
                    failures += 1
                print(f"  {mark} {i}. {label:<34} 实得 {got:<9} 期望 {want}")
    finally:
        server.terminate()
        server.wait(timeout=10)

    if keep:
        print(f"  store 保留在 {store}（含 server.log）")
    return failures == 0


def main() -> int:
    parser = argparse.ArgumentParser(description="HSBC 场景调试器")
    parser.add_argument("scenarios", nargs="*", help="场景编号，如 01 05 09")
    parser.add_argument("--all", action="store_true", help="跑全部")
    parser.add_argument("--list", action="store_true", help="只列出场景")
    parser.add_argument("--keep", action="store_true", help="保留 store 目录")
    args = parser.parse_args()

    if args.list:
        for key, scenario in SCENARIOS.items():
            print(f"{key}  {scenario['title']:<34} {scenario['file']}")
        return 0

    if not SERVER.exists():
        print(f"找不到服务端二进制：{SERVER}\n先构建：cargo build --release -p dogwood-server", file=sys.stderr)
        return 1
    if not (HELPER / "Cargo.toml").exists():
        print(f"找不到装策略的小工具：{HELPER}\n"
              f"先跑一次 python-demo/test_with_real_engine.sh 生成它（或设 DW_HELPER_DIR）", file=sys.stderr)
        return 1

    keys = list(SCENARIOS) if args.all else args.scenarios
    if not keys:
        parser.print_help()
        return 1
    unknown = [k for k in keys if k not in SCENARIOS]
    if unknown:
        print(f"没有这些场景：{unknown}（--list 看全部）", file=sys.stderr)
        return 1

    results = {key: run_scenario(key, args.keep) for key in keys}
    print(f"\n{'=' * 78}")
    for key, ok in results.items():
        print(f"场景 {key}: {'通过' if ok else '**有断言不符**'}")
    failed = [k for k, ok in results.items() if not ok]
    if failed:
        print(f"\n{len(failed)} 个场景有偏差：{failed}")
        return 1
    print("\n全部场景符合预期。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
