"""两边共用的事件脚本 —— **只有数据，没有任何引擎逻辑**。

两个实现目录各自读这里，保证"跑的是同一串事件"：

    dogwood/run_dogwood.py   把事件 submit 给真引擎
    opa/run_opa.py           自己维护历史，随每次求值传给 Rego

规则：转账 ≤ 10000 放行；> 10000 必须有同一金额、窗口内、已批准的历史。
窗口故意取 5 秒（而不是 1 小时），好让"滑出窗口"能在几秒内演示出来。
"""

from __future__ import annotations

PRINCIPAL = 'HSBC::User::"alice"'
RESOURCE = 'HSBC::Account::"main"'

ACTION_TRANSFER = 'HSBC::Action::transfer'
ACTION_APPROVE = 'HSBC::Action::approve_transfer'

#: 事件脚本。`wait_seconds` 表示执行这一步之前先等若干秒（用来演示窗口过期）。
STEPS: list[dict] = [
    {"label": "1) transfer 20000（无批准）", "action": "transfer", "kind": "request",
     "amount": 20000},
    {"label": "2) approve 20000（历史事件）", "action": "approve_transfer", "kind": "response",
     "amount": 20000, "approved": True},
    {"label": "3) transfer 20000（有批准）", "action": "transfer", "kind": "request",
     "amount": 20000},
    {"label": "4) transfer 20000（等 6 秒后）", "action": "transfer", "kind": "request",
     "amount": 20000, "wait_seconds": 6},
]

#: 只有 OPA 侧需要的"调用方出错"变体（Dogwood 没有这种失败模式）。
VARIANTS = ["full", "dropped", "stale"]


def transfer_fields(amount: int) -> dict:
    return {"amount": amount, "payee": "ACME-001", "payee_type": "internal"}
