#!/usr/bin/env python3
"""Dogwood HTTP 测试用例集：**写入规则 → 逐条验证裁决**。

服务启动后（all-in-one 或 Compose 都行）直接跑：

    export DOGWOOD_CONTROL_TOKEN=<你的控制面 token>
    python3 dogwood-http/examples/testcases.py --all      # 跑全部
    python3 dogwood-http/examples/testcases.py 01 07 10   # 只跑指定用例
    python3 dogwood-http/examples/testcases.py --list     # 看有哪些用例

默认打 http://127.0.0.1:8080（数据面）与 http://127.0.0.1:8081（控制面），可用
`--data-url` / `--control-url` / `--token` 或环境变量 DOGWOOD_DATA_URL /
DOGWOOD_CONTROL_URL / DOGWOOD_CONTROL_TOKEN / DOGWOOD_DATA_TOKEN 覆盖。

隔离性：每个用例换一个**全新 principal**（引擎的历史按 principal 分键），并且重新
安装自己的策略集，所以用例之间互不污染，**同一个服务上可以反复运行**。

退出码：全部通过 0，有失败 1。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
POLICY_DIR = ROOT / "python-demo" / "hsbc" / "policies"
SCHEMA_FILE = ROOT / "python-demo" / "hsbc" / "schema.cedarschema"

RESOURCE = 'HSBC::Account::"main"'
ACTION_TRANSFER = 'HSBC::Action::transfer'
ACTION_APPROVE = 'HSBC::Action::approve_transfer'
ACTION_READ = 'HSBC::Action::read_customer'
ACTION_VERIFY = 'HSBC::Action::verify_customer'


# ── 输出 ────────────────────────────────────────────────────────────────────

class Reporter:
    def __init__(self) -> None:
        self.passed = 0
        self.failed = 0
        self._tty = sys.stdout.isatty()

    def _paint(self, text: str, code: str) -> str:
        return f"\033[{code}m{text}\033[0m" if self._tty else text

    def ok(self, msg: str) -> None:
        print(f"  {self._paint('ok', '32')}   {msg}")
        self.passed += 1

    def fail(self, msg: str, detail: str = "") -> None:
        print(f"  {self._paint('FAIL', '31')} {msg}")
        if detail:
            print(f"       {detail}")
        self.failed += 1

    def check(self, cond: bool, msg: str, detail: str = "") -> bool:
        if cond:
            self.ok(msg)
        else:
            self.fail(msg, detail)
        return cond


# ── HTTP 客户端 ─────────────────────────────────────────────────────────────

class Client:
    def __init__(self, data_url: str, control_url: str,
                 control_token: str, data_token: str | None) -> None:
        self.data_url = data_url.rstrip("/")
        self.control_url = control_url.rstrip("/")
        self.control_token = control_token
        self.data_token = data_token
        # reason 里的 token → 规则名 的缓存，避免同一个 token 反复反查。
        self._rule_cache: dict[str, str] = {}

    def call(self, method: str, url: str, body=None, token: str | None = None):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        if body is not None:
            req.add_header("Content-Type", "application/json")
        if token:
            req.add_header("Authorization", f"Bearer {token}")
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                raw = resp.read()
                return resp.status, (json.loads(raw) if raw else {})
        except urllib.error.HTTPError as err:
            raw = err.read()
            try:
                return err.code, (json.loads(raw) if raw else {})
            except Exception:
                return err.code, {}
        except urllib.error.URLError as err:
            raise SystemExit(
                f"连不上 {url}（{err.reason}）。服务起来了吗？URL 对吗？"
            )

    # 数据面
    def ping(self):
        return self.call("GET", f"{self.data_url}/v1/ping")

    def submit(self, event):
        return self.call("POST", f"{self.data_url}/v1/events", event, self.data_token)

    # 控制面
    def install(self, policy: str, schema: str):
        return self.call("POST", f"{self.control_url}/v1/policy",
                         {"policy": policy, "action_schema": schema}, self.control_token)

    def verbs(self, verbs: list):
        return self.call("POST", f"{self.control_url}/v1/policy/verbs",
                         {"verbs": verbs}, self.control_token)

    def status(self):
        return self.call("GET", f"{self.control_url}/v1/status", token=self.control_token)

    def status_no_token(self):
        return self.call("GET", f"{self.control_url}/v1/status")

    def policy_by_id(self, token: str):
        return self.call("GET", f"{self.control_url}/v1/policies/{token}",
                         token=self.control_token)

    def rule_name(self, token: str) -> str:
        """把裁决 `reason` 里的策略 token 反查成 `@id(...)` 规则名。"""
        if token not in self._rule_cache:
            code, resp = self.policy_by_id(token)
            source = resp.get("source", "") if isinstance(resp, dict) else ""
            match = re.search(r'@id\("([^"]+)"\)', source)
            self._rule_cache[token] = match.group(1) if match else f"{token}(?)"
        return self._rule_cache[token]


# ── 事件与裁决 ──────────────────────────────────────────────────────────────

def print_exchange(client: Client, event: dict, code: int, resp) -> None:
    """打印一次 allow/deny 的完整执行内容：请求体、响应体、决定的规则。"""
    print(f"       → 请求: {json.dumps(event, ensure_ascii=False)}")
    print(f"       ← 响应: http {code} {json.dumps(resp, ensure_ascii=False)}")
    reasons = resp.get("reason") if isinstance(resp, dict) else None
    if reasons:
        names = ", ".join(client.rule_name(token) for token in reasons)
        print(f"         决定规则: {names}")


def _indent(text: str, prefix: str = "         ") -> str:
    return "\n".join(prefix + line for line in text.rstrip("\n").splitlines())


def build_event(principal, action, kind, inp, out=None):
    logged = {"input": inp}
    if out is not None:
        logged["output"] = out
    return {"action": action, "kind": kind, "principal": principal,
            "resource": RESOURCE, "logged": logged, "context": {"input": inp}}


def verdict(resp):
    status = resp.get("status")
    if status == "recorded":
        return "recorded"
    if status == "decision":
        return "allow" if resp.get("allowed") else "deny"
    return f"?{status}"


def expected_http(expect: str) -> int:
    # deny 是 403（fail-closed），其余成功路径是 200
    return 403 if expect == "deny" else 200


# ── 步骤构造器（与 python-demo/hsbc 一致） ───────────────────────────────────

def transfer(amount, expect, payee="ACME-001", payee_type="internal"):
    return {"label": f"transfer {amount} → {payee} ({payee_type})", "expect": expect,
            "action": ACTION_TRANSFER, "kind": "request",
            "inp": {"amount": amount, "payee": payee, "payee_type": payee_type}, "out": None}


def transfer_settled(amount, payee="ACME-001", payee_type="internal"):
    return {"label": f"settle transfer {amount}（response，只记历史）", "expect": "recorded",
            "action": ACTION_TRANSFER, "kind": "response",
            "inp": {"amount": amount, "payee": payee, "payee_type": payee_type},
            "out": {"reference": "TR-SETTLED"}}


def approve(amount, approved=True, approver="bob"):
    return {"label": f"approve {amount}（response）", "expect": "recorded",
            "action": ACTION_APPROVE, "kind": "response",
            "inp": {"amount": amount, "approver": approver}, "out": {"approved": approved}}


def read_customer(classification="confidential"):
    return {"label": f"read_customer（response, {classification}）", "expect": "recorded",
            "action": ACTION_READ, "kind": "response",
            "inp": {"customer_id": "C-1001"}, "out": {"classification": classification}}


def read_request(expect):
    return {"label": "read_customer（请求）", "expect": expect,
            "action": ACTION_READ, "kind": "request",
            "inp": {"customer_id": "C-1001"}, "out": None}


def verify(verified):
    return {"label": f"verify_customer（response, verified={verified}）", "expect": "recorded",
            "action": ACTION_VERIFY, "kind": "response",
            "inp": {"customer_id": "C-1001"}, "out": {"verified": verified}}


# ── 用例 ────────────────────────────────────────────────────────────────────

def policy_cases():
    return {
        "01": {"file": "01_dual_approval.dw",
               "title": "maker-checker：大额必须有同额批准（formerly within 1h）",
               "steps": [transfer(5000, "allow"), transfer(20000, "deny"), approve(20000),
                         transfer(20000, "allow"), transfer(20000, "allow")]},
        "02": {"file": "02_rate_limit.dw",
               "title": "频率限制：一小时最多 5 笔（count_within + ::request）",
               "steps": [transfer(1000, "allow" if i <= 5 else "deny") for i in range(1, 7)]},
        "03": {"file": "03_cumulative_cap.dw",
               "title": "累计额度：一小时累计 ≤ 50000（sum_within）",
               "steps": [transfer(20000, "allow"), transfer(20000, "allow"), transfer(20000, "deny")]},
        "04": {"file": "04_anti_spike.dw",
               "title": "反异常：单笔不得超过此前已结算总额（bind + 聚合）",
               "steps": [transfer_settled(1000), transfer(500, "allow"),
                         transfer(2000, "deny"), transfer(800, "allow")]},
        "05": {"file": "05_pii_external_block.dw",
               "title": "信息隔离：读机密后 8h 内禁止对外转账（formerly + 输入过滤）",
               "steps": [transfer(100, "allow", payee_type="external"), read_customer("confidential"),
                         transfer(100, "deny", payee_type="external"),
                         transfer(100, "allow", payee_type="internal")]},
        "06": {"file": "06_distinct_payees.dw",
               "title": "分散度：一小时最多 3 个不同收款人（count_distinct_within）",
               "steps": [transfer(10, "allow", payee="bob"), transfer(10, "allow", payee="carol"),
                         transfer(10, "allow", payee="dave"), transfer(10, "deny", payee="erin"),
                         transfer(10, "deny", payee="bob")]},
        "07": {"file": "07_sanctions_forbid.dw",
               "title": "硬禁名单：forbid 覆盖 permit（纯 Cedar）",
               "steps": [transfer(100, "allow", payee="ACME-001"),
                         transfer(100, "deny", payee="SANCTIONED-001")]},
        "08": {"file": "08_verified_session.dw",
               "title": "会话有效性：!失败 since 成功（since + 否定左操作数）",
               "steps": [read_request("deny"), verify(True), read_request("allow"),
                         verify(False), read_request("deny")]},
        "09": {"file": "09_consume_approval.dw",
               "title": "一次批准只能消费一次（聚合对聚合）",
               "steps": [approve(20000), transfer(20000, "allow"), transfer(20000, "deny")]},
    }


def run_contracts(client: Client, rep: Reporter, verbose: bool) -> None:
    print("\n[00] 服务契约：ping / 鉴权 / 拒错字段")
    code, resp = client.ping()
    rep.check(code == 200, f"数据面 ping 可达 → http {code}（version={resp.get('version', '?')}）",
              f"http {code} {resp}")
    code, _ = client.status_no_token()
    rep.check(code == 401, f"控制面无 token 被拒 → http {code}（期望 401）", f"got http {code}")
    code, resp = client.status()
    rep.check(code == 200, f"控制面带 token 可读 status（{resp.get('rule_count', '?')} 条规则）",
              f"http {code} {resp}")
    bad = {"action": ACTION_TRANSFER, "kind": "request", "ts": 1}
    code, resp = client.submit(bad)
    if verbose:
        print_exchange(client, bad, code, resp)
    rep.check(code == 400, f"未知字段被拒（不是静默丢弃）→ http {code}（期望 400）", f"got http {code}")


def run_policy_case(client: Client, schema: str, key: str, spec: dict,
                    rep: Reporter, nonce: str, verbose: bool) -> None:
    principal = f'HSBC::User::"alice-{key}-{nonce}"'
    print(f"\n[{key}] {spec['title']}")
    policy = (POLICY_DIR / spec["file"]).read_text()
    if verbose:
        print(f"       策略源码（{spec['file']}）:\n{_indent(policy)}")
    code, resp = client.install(policy, schema)
    if not rep.check(code == 200,
                     f"写入规则：安装 {spec['file']}（{resp.get('rule_count', '?')} 条规则）",
                     f"http {code} {resp}"):
        return
    for step in spec["steps"]:
        event = build_event(principal, step["action"], step["kind"], step["inp"], step["out"])
        code, resp = client.submit(event)
        got = verdict(resp)
        want = step["expect"]
        if verbose:
            print_exchange(client, event, code, resp)
        rep.check(code == expected_http(want) and got == want,
                  f"{step['label']} → {got}（期望 {want}, http {code}）",
                  f"期望 {want}/{expected_http(want)}，实际 {got}/{code}：{resp}")


def run_runtime_editing(client: Client, schema: str, rep: Reporter, nonce: str,
                        verbose: bool) -> None:
    principal = f'HSBC::User::"alice-10-{nonce}"'
    print("\n[10] 运行时写入/删除规则（POST /v1/policy/verbs，无需重启）")

    base = '@id("allow_all_transfer")\npermit (principal, action == HSBC::Action::"transfer", resource);\n'
    if verbose:
        print(f"       基线策略:\n{_indent(base)}")
    code, resp = client.install(base, schema)
    if not rep.check(code == 200, "写入规则：安装基线策略（放行所有转账）", f"http {code} {resp}"):
        return

    blocked = {"amount": 100, "payee": "BLOCKED-001", "payee_type": "internal"}
    event = build_event(principal, ACTION_TRANSFER, "request", blocked)
    code, resp = client.submit(event)
    if verbose:
        print_exchange(client, event, code, resp)
    rep.check(code == 200 and verdict(resp) == "allow",
              f"基线：转给 BLOCKED-001 → {verdict(resp)}（期望 allow, http {code}）", f"{resp}")

    forbid = ('@id("block_blocked")\n'
              'forbid (principal, action == HSBC::Action::"transfer", resource)\n'
              'when { context.input.payee == "BLOCKED-001" };\n')
    if verbose:
        print(f"       新增规则（verb=add）:\n{_indent(forbid)}")
    code, resp = client.verbs([{"verb": "add", "policy": forbid}])
    minted = resp.get("minted", []) if isinstance(resp, dict) else []
    rep.check(code == 200 and minted, f"新增一条 forbid 规则 → 分配 id {minted}", f"http {code} {resp}")

    event = build_event(principal, ACTION_TRANSFER, "request", blocked)
    code, resp = client.submit(event)
    if verbose:
        print_exchange(client, event, code, resp)
    rep.check(code == 403 and verdict(resp) == "deny",
              f"新规则立即生效：转给 BLOCKED-001 → {verdict(resp)}（期望 deny, http {code}）", f"{resp}")

    if minted:
        code, resp = client.verbs([{"verb": "delete", "id": minted[0]}])
        rep.check(code == 200, f"删除这条 forbid 规则 → http {code}", f"{resp}")
        event = build_event(principal, ACTION_TRANSFER, "request", blocked)
        code, resp = client.submit(event)
        if verbose:
            print_exchange(client, event, code, resp)
        rep.check(code == 200 and verdict(resp) == "allow",
                  f"删除后恢复：转给 BLOCKED-001 → {verdict(resp)}（期望 allow, http {code}）", f"{resp}")


# ── 入口 ────────────────────────────────────────────────────────────────────

def token_from_dotenv() -> str | None:
    env_file = ROOT / ".env"
    if not env_file.is_file():
        return None
    for line in env_file.read_text().splitlines():
        if line.startswith("DOGWOOD_CONTROL_TOKEN="):
            return line.split("=", 1)[1].strip()
    return None


def main() -> int:
    parser = argparse.ArgumentParser(description="Dogwood HTTP 测试用例集")
    parser.add_argument("cases", nargs="*", help="用例编号，如 01 07 10；留空 = 全部")
    parser.add_argument("--all", action="store_true", help="跑全部用例")
    parser.add_argument("--list", action="store_true", help="列出所有用例后退出")
    parser.add_argument("--data-url", default=os.environ.get("DOGWOOD_DATA_URL", "http://127.0.0.1:8080"))
    parser.add_argument("--control-url", default=os.environ.get("DOGWOOD_CONTROL_URL", "http://127.0.0.1:8081"))
    parser.add_argument("--token", default=os.environ.get("DOGWOOD_CONTROL_TOKEN"))
    parser.add_argument("--data-token", default=os.environ.get("DOGWOOD_DATA_TOKEN"))
    parser.add_argument("-v", "--verbose", action="store_true",
                        help="打印每次 allow/deny 的请求体、响应体，以及决定的规则名")
    args = parser.parse_args()

    cases = policy_cases()
    available = ["00"] + sorted(cases) + ["10"]

    if args.list:
        print("可用用例：")
        print("  00  服务契约：ping / 鉴权 / 拒错字段")
        for key in sorted(cases):
            print(f"  {key}  {cases[key]['title']}")
        print("  10  运行时写入/删除规则（/v1/policy/verbs）")
        return 0

    selected = args.cases if args.cases else available
    unknown = [c for c in selected if c not in available]
    if unknown:
        print(f"未知用例：{', '.join(unknown)}。可用：{' '.join(available)}", file=sys.stderr)
        return 2

    token = args.token or token_from_dotenv()
    if not token:
        print("缺少控制面 token：设置 DOGWOOD_CONTROL_TOKEN，或用 --token 传入，"
              "或在仓库根放 .env。", file=sys.stderr)
        return 2

    client = Client(args.data_url, args.control_url, token, args.data_token)
    schema = SCHEMA_FILE.read_text()
    nonce = f"{int(time.time())}"
    rep = Reporter()

    print(f"Dogwood HTTP 测试用例集   数据面={client.data_url}  控制面={client.control_url}")
    try:
        if "00" in selected:
            run_contracts(client, rep, args.verbose)
        for key in sorted(cases):
            if key in selected:
                run_policy_case(client, schema, key, cases[key], rep, nonce, args.verbose)
        if "10" in selected:
            run_runtime_editing(client, schema, rep, nonce, args.verbose)
    except SystemExit:
        raise

    print()
    print(f"{rep.passed} passed, {rep.failed} failed")
    return 1 if rep.failed else 0


if __name__ == "__main__":
    sys.exit(main())
