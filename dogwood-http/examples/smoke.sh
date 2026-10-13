#!/usr/bin/env bash
#
# End-to-end smoke test for the Docker deployment: a real engine, a real temporal
# policy, and the HTTP adapter in front of both.
#
# It asserts the thing the whole design is about. The demo policy says a refund needs
# a matching approval within the hour, so:
#
#   1. a refund with no approval          → 403, and 403 is not a 2xx
#   2. the approval tool's response lands → 200, recorded, no verdict
#   3. the same refund now                → 200, allowed, naming the determining rule
#   4. a refund for an amount nobody approved → 403
#
# The amount is generated per run, so a second run starts from a clean slate rather
# than inheriting the first run's approval.
#
# Usage:  ./dogwood-http/examples/smoke.sh
#         DOGWOOD_CONTROL_URL=http://127.0.0.1:9081 ./dogwood-http/examples/smoke.sh

set -euo pipefail

HERE=$(cd "$(dirname "$0")" && pwd)
ROOT=$(cd "$HERE/../.." && pwd)

DATA_URL=${DOGWOOD_DATA_URL:-http://127.0.0.1:8080}
CONTROL_URL=${DOGWOOD_CONTROL_URL:-http://127.0.0.1:8081}

# The token comes from the environment, or from the `.env` the container was started
# with (`docker run --env-file .env …`).
if [[ -z ${DOGWOOD_CONTROL_TOKEN:-} && -f "$ROOT/.env" ]]; then
    DOGWOOD_CONTROL_TOKEN=$(sed -n 's/^DOGWOOD_CONTROL_TOKEN=//p' "$ROOT/.env" | tail -1)
fi
: "${DOGWOOD_CONTROL_TOKEN:?set DOGWOOD_CONTROL_TOKEN, or create .env with DOGWOOD_CONTROL_TOKEN=<token>}"

# A fresh amount each run, so "no approval for this amount" is always true at step 1.
AMOUNT=$(( (RANDOM * 32768 + RANDOM) % 100000 + 1000 ))

pass=0
fail=0

# `status` runs inside `$( )`, so it cannot communicate through a shell variable —
# the last body lands in this file, which the caller can then read.
BODY_FILE=$(mktemp)
trap 'rm -f "$BODY_FILE"' EXIT

status() { # status <curl args...> → prints the status code; body goes to $BODY_FILE
    curl -s -o "$BODY_FILE" -w '%{http_code}' "$@"
}

expect() { # expect <code> <what>
    local want=$1 what=$2 got=$3
    if [[ "$got" == "$want" ]]; then
        printf '  ok   %s (%s)\n' "$what" "$got"
        pass=$((pass + 1))
    else
        printf '  FAIL %s — expected %s, got %s\n       %s\n' "$what" "$want" "$got" "$(cat "$BODY_FILE")"
        fail=$((fail + 1))
    fi
}

event() { # event <action> <kind> <amount> [approved] → body on stdout
    local action=$1 kind=$2 amount=$3 approved=${4:-}
    if [[ -n "$approved" ]]; then
        printf '{"action":"%s","kind":"%s","principal":"AgentCore::User::\\"alice\\"","resource":"AgentCore::Account::\\"main\\"","logged":{"input":{"amount":%s},"output":{"approved":%s}},"context":{"input":{"amount":%s}}}' \
            "$action" "$kind" "$amount" "$approved" "$amount"
    else
        printf '{"action":"%s","kind":"%s","principal":"AgentCore::User::\\"alice\\"","resource":"AgentCore::Account::\\"main\\"","logged":{"input":{"amount":%s}},"context":{"input":{"amount":%s}}}' \
            "$action" "$kind" "$amount" "$amount"
    fi
}

echo "Dogwood smoke test  (amount=${AMOUNT})"
echo

# ─── the control plane, which is the half that needs Linux ───────────
echo "control plane:"

INSTALL=$(mktemp)
python3 - "$ROOT" > "$INSTALL" <<'PY'
import json, sys
root = sys.argv[1]
with open(f"{root}/python-demo/demo_policy.dw") as f:
    policy = f.read()
with open(f"{root}/python-demo/demo_schema.cedarschema") as f:
    schema = f.read()
print(json.dumps({"policy": policy, "action_schema": schema}))
PY

expect 200 "a policy set installs atomically" \
    "$(status -X POST -H "Authorization: Bearer $DOGWOOD_CONTROL_TOKEN" \
        -H 'Content-Type: application/json' --data-binary "@$INSTALL" "$CONTROL_URL/v1/policy")"
rm -f "$INSTALL"

expect 401 "a control route refuses an unauthenticated caller" \
    "$(status "$CONTROL_URL/v1/status")"
expect 200 "status is readable with the token" \
    "$(status -H "Authorization: Bearer $DOGWOOD_CONTROL_TOKEN" "$CONTROL_URL/v1/status")"

# ─── the data plane, which is the half that carries the verdicts ─────
echo
echo "data plane:"

# NB: action names on the wire carry no quotes. The policy writes
# `AgentCore::Action::"refund"`; the event field is `AgentCore::Action::refund`.
# Getting this wrong looks exactly like a broken policy: every request is denied.
expect 403 "a refund with no matching approval is denied" \
    "$(status -X POST -H 'Content-Type: application/json' \
        --data-binary "$(event AgentCore::Action::refund request "$AMOUNT")" \
        "$DATA_URL/v1/events")"

expect 200 "the approval's response event is recorded (no verdict)" \
    "$(status -X POST -H 'Content-Type: application/json' \
        --data-binary "$(event AgentCore::Action::approve response "$AMOUNT" true)" \
        "$DATA_URL/v1/events")"

expect 200 "the same refund is now allowed" \
    "$(status -X POST -H 'Content-Type: application/json' \
        --data-binary "$(event AgentCore::Action::refund request "$AMOUNT")" \
        "$DATA_URL/v1/events")"

expect 403 "a refund of an unapproved amount is still denied" \
    "$(status -X POST -H 'Content-Type: application/json' \
        --data-binary "$(event AgentCore::Action::refund request "$((AMOUNT + 1))")" \
        "$DATA_URL/v1/events")"

# ─── the contracts the deployment rests on ───────────────────────────
echo
echo "contracts:"

expect 400 "an unknown event field is refused, not silently dropped" \
    "$(status -X POST -H 'Content-Type: application/json' \
        --data-binary '{"action":"AgentCore::Action::refund","kind":"request","ts":1}' \
        "$DATA_URL/v1/events")"

for url in "$DATA_URL/docs" "$CONTROL_URL/docs"; do
    code=$(status "$url")
    # 303 is the UI redirecting to /docs/; both are "served".
    if [[ "$code" == 200 || "$code" == 303 ]]; then
        printf '  ok   %s serves the Swagger UI (%s)\n' "$url" "$code"
        pass=$((pass + 1))
    else
        printf '  FAIL %s did not serve the UI (%s)\n' "$url" "$code"
        fail=$((fail + 1))
    fi
done

echo
printf '%d passed, %d failed\n' "$pass" "$fail"
[[ "$fail" -eq 0 ]]
