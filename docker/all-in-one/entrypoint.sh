#!/usr/bin/env bash
#
# All-in-one entrypoint: run the Dogwood engine and the HTTP adapter
# (both planes, one process) inside a single container, as one uid.
#
# This is the *convenient* topology: one container, one uid, no cross-uid or
# cross-PID-namespace constraints to satisfy, and a plain `docker run` needs no
# Compose file. It is deliberately NOT the tamper-resistant topology: because the
# adapter serves both planes from one process, the process that accepts the
# agent's events can also rewrite the policy set (dogwood-http prints that warning
# at startup). Use the three-service Compose layout when that boundary matters.
#
# Environment:
#   DOGWOOD_CONTROL_TOKEN       required — bearer token for the control plane
#   DOGWOOD_CONTROL_TOKEN_FILE   alternative to the above (Docker/K8s secrets)
#   DOGWOOD_DATA_TOKEN          optional — require a token on the data plane too
#   DOGWOOD_STORE_DIR           engine state dir           (default /var/lib/dogwood)
#   DOGWOOD_DATA_BIND           data-plane listen address  (default 0.0.0.0:8080)
#   DOGWOOD_CONTROL_BIND        control-plane listen addr  (default 0.0.0.0:8081)
#   DOGWOOD_SNAPSHOT_INTERVAL   events between snapshots   (default 10000; 0 = off)
#   DOGWOOD_POLICY_FILE         optional — install this .dw policy on startup
#   DOGWOOD_ACTION_SCHEMA_FILE  required with DOGWOOD_POLICY_FILE (.cedarschema)
#   DOGWOOD_EVENT_SCHEMA_FILE   optional with DOGWOOD_POLICY_FILE (.dwschema)
#   DOGWOOD_LOG                 adapter log filter (default dogwood_http=info,warn)

set -euo pipefail

DW_DIR="${DOGWOOD_STORE_DIR:-/var/lib/dogwood}"
DATA_BIND="${DOGWOOD_DATA_BIND:-0.0.0.0:8080}"
CONTROL_BIND="${DOGWOOD_CONTROL_BIND:-0.0.0.0:8081}"
SNAPSHOT_INTERVAL="${DOGWOOD_SNAPSHOT_INTERVAL:-10000}"

engine_pid=""
http_pid=""

log() { printf 'dogwood-entrypoint: %s\n' "$*" >&2; }

shutdown() {
    trap - TERM INT
    # Best-effort clean checkpoint so a restart replays less of the log. Not
    # required for correctness — the log is durable and replay is what recovers it.
    if [ -n "$engine_pid" ] && kill -0 "$engine_pid" 2>/dev/null; then
        dogwood-server --dir "$DW_DIR" checkpoint >/dev/null 2>&1 || true
    fi
    [ -n "$http_pid" ] && kill -TERM "$http_pid" 2>/dev/null || true
    [ -n "$engine_pid" ] && kill -TERM "$engine_pid" 2>/dev/null || true
    wait 2>/dev/null || true
}

trap shutdown TERM INT

if [ -z "${DOGWOOD_CONTROL_TOKEN:-}" ] && [ -z "${DOGWOOD_CONTROL_TOKEN_FILE:-}" ]; then
    log "DOGWOOD_CONTROL_TOKEN (or DOGWOOD_CONTROL_TOKEN_FILE) is required: --plane both serves the control plane."
    log "generate one with:  openssl rand -hex 32"
    exit 64
fi

mkdir -p "$DW_DIR"

log "starting engine (store: $DW_DIR, snapshot every $SNAPSHOT_INTERVAL events)"
dogwood-server --dir "$DW_DIR" run --snapshot-interval "$SNAPSHOT_INTERVAL" &
engine_pid=$!

# Wait for the engine to answer on its data socket before starting the adapter, so
# the first requests are not 502s.
ready=0
for _ in $(seq 1 200); do
    if dogwood-server --dir "$DW_DIR" ping >/dev/null 2>&1; then
        ready=1
        break
    fi
    if ! kill -0 "$engine_pid" 2>/dev/null; then
        log "engine exited during startup"
        exit 1
    fi
    sleep 0.1
done
if [ "$ready" -ne 1 ]; then
    log "engine did not become ready within 20s"
    exit 1
fi

# Optional: install a policy set on startup. This *replaces* the whole set every
# start, so leave it unset if you author policy through the control API.
if [ -n "${DOGWOOD_POLICY_FILE:-}" ]; then
    if [ -z "${DOGWOOD_ACTION_SCHEMA_FILE:-}" ]; then
        log "DOGWOOD_POLICY_FILE is set but DOGWOOD_ACTION_SCHEMA_FILE is not; --action-schema is required for install"
        exit 64
    fi
    install_args=(policy install "$DOGWOOD_POLICY_FILE" --action-schema "$DOGWOOD_ACTION_SCHEMA_FILE")
    if [ -n "${DOGWOOD_EVENT_SCHEMA_FILE:-}" ]; then
        install_args+=(--event-schema "$DOGWOOD_EVENT_SCHEMA_FILE")
    fi
    log "installing policy $DOGWOOD_POLICY_FILE"
    dogwood-server --dir "$DW_DIR" "${install_args[@]}"
fi

log "starting HTTP adapter (both planes): data=$DATA_BIND control=$CONTROL_BIND"
dogwood-http serve \
    --plane both \
    --data-socket "$DW_DIR/run/dogwood.sock" \
    --control-socket "$DW_DIR/control.sock" \
    --data-bind "$DATA_BIND" \
    --control-bind "$CONTROL_BIND" \
    --allow-non-loopback-control &
http_pid=$!

# Exit as soon as either process dies, then take the other down with us.
set +e
wait -n "$engine_pid" "$http_pid"
rc=$?
set -e
log "a child exited (status $rc); shutting down"
shutdown
exit "$rc"
