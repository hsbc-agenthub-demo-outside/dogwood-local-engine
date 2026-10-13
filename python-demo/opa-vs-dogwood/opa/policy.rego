# 对比用策略（OPA/Rego 版）：**同一规则，但时序要靠调用方把历史传进来**。
#
# Rego 每次求值都是无状态的：`input` 里有什么，它就只能看什么。所以窗口过滤、
# 同额匹配、"是否已批准"这些全部要在这里手写，而 input.history 由调用方组装。

package dogwood_compare

import rego.v1

default allow := false

#: 与 policy.dw 里的 `within 5s` 对齐（单位：纳秒）
window_ns := 5 * 1000000000

# 小额直接放行 —— 对应 @id("cmp_small_transfer")
allow if {
	input.request.amount <= 10000
}

# 大额需要窗口内、同额、已批准的历史 —— 对应 @id("cmp_large_transfer_needs_approval")
allow if {
	input.request.amount > 10000
	some approval in input.history
	approval.kind == "approve_response"
	approval.approved == true
	approval.amount == input.request.amount
	age := input.now - approval.ts
	age >= 0
	age <= window_ns
}

# 归因：哪条规则让它通过的（对应 Dogwood 响应里的 reason）
reason contains "cmp_small_transfer" if {
	allow
	input.request.amount <= 10000
}

reason contains "cmp_large_transfer_needs_approval" if {
	allow
	input.request.amount > 10000
}

decision := {"allow": allow, "reason": reason}
