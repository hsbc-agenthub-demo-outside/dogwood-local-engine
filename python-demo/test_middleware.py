"""不依赖 LLM 的最小验证：拦截器本身的行为。

跑法：``python3 test_middleware.py``。用离线替身，不需要 Rust，也不需要 API key。
"""

from __future__ import annotations

import os
import sys
import tempfile

from dogwood_client import ControlClient, DogwoodClient, describe, is_allowed
from dogwood_middleware import guarded_tool_call
from dogwood_stub_server import StubServer

HERE = os.path.dirname(os.path.abspath(__file__))


def _read(name: str) -> str:
    with open(os.path.join(HERE, name), encoding="utf-8") as fh:
        return fh.read()


def main() -> int:
    calls: list[int] = []

    def run_refund(args: dict) -> dict:
        calls.append(args["amount"])
        return {"refunded": True, "amount": args["amount"]}

    with tempfile.TemporaryDirectory() as tmp:
        stub = StubServer(tmp).start()
        control = ControlClient(str(stub.control_socket))
        data = DogwoodClient(str(stub.data_socket))
        try:
            control.install(_read("demo_policy.dw"), _read("demo_schema.cedarschema"))
            kw = {"principal": 'AgentCore::User::"alice"'}

            # 1) 没有批准历史 -> 拒绝，且**工具必须没被执行**
            denied = guarded_tool_call(data, "refund", {"amount": 500}, run_refund, **kw)
            assert denied["allowed"] is False, denied
            assert calls == [], f"被拒绝的工具被误执行了：{calls}"
            print(f"  deny  : {describe(denied['verdict'])} (tool calls={calls})")

            # 2) 记录一条批准 -> 只是 history，不进当前决策
            recorded = data.submit(
                'AgentCore::Action::approve', "response",
                logged={"input": {"amount": 500}, "output": {"approved": True}},
            )
            assert recorded.get("status") == "recorded", recorded
            print(f"  record: {describe(recorded)}")

            # 3) 现在同一金额有历史 -> 放行，且工具真的被调用了一次
            allowed = guarded_tool_call(data, "refund", {"amount": 500}, run_refund, **kw)
            assert allowed["allowed"] is True, allowed
            assert calls == [500], f"放行但工具没执行：{calls}"
            print(f"  allow : {describe(allowed['verdict'])} (tool calls={calls})")

            # 4) 金额不同 -> 那条批准不适用（相关性是按字段匹配的）
            other = data.submit(
                'AgentCore::Action::refund', "request",
                context={"input": {"amount": 999}}, logged={"input": {"amount": 999}},
            )
            assert is_allowed(other) is False, other
            print(f"  other : {describe(other)} (另一金额被拒)")

            # 5) 未知字段（含时间戳）必须被服务端拒绝 —— 客户端不能自己定时间
            bad = data.call({"op": "submit", "event": {"action": "A", "kind": "request", "ts": 1}})
            assert bad.get("status") == "error", bad
            print(f"  reject: {bad['message']}")

            # 6) 传了未知 op -> error，且按 fail closed 读就是"不通过"
            assert is_allowed(data.call({"op": "nonesuch"})) is False
            print("  fail  : 未知 op 不是 allow（fail closed）")
        finally:
            data.close()
            control.close()
            stub.stop()

    print("\n全部通过。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
