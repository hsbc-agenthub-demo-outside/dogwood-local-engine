#!/usr/bin/env bash
#
# Socket all-in-one entrypoint: the engine and a ready-to-use Python client in one
# container, so the socket deployment needs no Compose file.
#
# This is the *convenient* topology, like `docker/all-in-one/entrypoint.sh` is for the
# HTTP image — with one difference worth knowing about, because it cuts the other way:
#
#   The HTTP all-in-one serves both planes from one process, so the process that
#   takes the agent's events can also rewrite the policy set. The boundary is gone.
#
#   The socket all-in-one does not have that problem. The engine's control socket is
#   mode 0700 and the engine authorizes its callers by kernel peer credential, so a
#   caller running as a *different* uid still cannot author policy — inside one
#   container just as across three. What the single container does lose is that the
#   data-plane process can see the control socket's *name*; the three-service layout
#   does not even put it in that container's filesystem.
#
# Commands (first argument; default `run`):
#   run        serve the engine (default)
#   install    install a policy set, as the operator uid
#   demo       walk the refund story through the data socket
#   test       run the SDK's test suite against this container's engine
#   self-test  throwaway engine in a temp dir: install, demo, exit  (image smoke test)
#   help
#
# Environment:
#   DOGWOOD_STORE_DIR           engine state dir          (default /var/lib/dogwood)
#   DOGWOOD_AGENT_SOCKET        socket the agent reaches  (default /run/dogwood/dogwood.sock)
#   DOGWOOD_AGENT_UID           the agent's uid           (default 10002)
#   DOGWOOD_TCP_DATA_BIND       optional — also serve the data socket over TCP (host:port)
#   DOGWOOD_TCP_CONTROL_BIND    optional — also serve the control socket over TCP. Read the
#                               warning: the forwarder is what the engine authenticates, so
#                               anyone who can reach that port can author policy.
#   DOGWOOD_SNAPSHOT_INTERVAL   events between snapshots  (default 10000; 0 = off)
#   DOGWOOD_POLICY_FILE         optional — install on startup (.dw)
#   DOGWOOD_ACTION_SCHEMA_FILE  required with DOGWOOD_POLICY_FILE (.cedarschema)
#   DOGWOOD_EVENT_SCHEMA_FILE   optional with DOGWOOD_POLICY_FILE (.dwschema)
#   DOGWOOD_LOG                 SDK log filter (default dogwood_sdk=warning)

set -euo pipefail

DW_DIR="${DOGWOOD_STORE_DIR:-/var/lib/dogwood}"
AGENT_SOCKET="${DOGWOOD_AGENT_SOCKET:-/run/dogwood/dogwood.sock}"
AGENT_UID="${DOGWOOD_AGENT_UID:-10002}"
ENGINE_UID=10001
TCP_DATA_BIND="${DOGWOOD_TCP_DATA_BIND:-}"
TCP_CONTROL_BIND="${DOGWOOD_TCP_CONTROL_BIND:-}"
SNAPSHOT_INTERVAL="${DOGWOOD_SNAPSHOT_INTERVAL:-10000}"
EXAMPLES=/opt/dogwood/examples
SDK=/opt/dogwood-sdk

export DOGWOOD_STORE_DIR="$DW_DIR"
# The agent's path, which is *not* the engine's when the socket directory is mounted a
# second time at a traversable path. Operator-side commands derive theirs from the
# store directory; `demo` is the one that has to use this one.
export DOGWOOD_SOCKET="$AGENT_SOCKET"
export DOGWOOD_CONTROL_SOCKET="$DW_DIR/control.sock"
export PYTHONPATH="$SDK"
export PYTHONDONTWRITEBYTECODE=1

engine_pid=""
bridge_pids=()

log() { printf 'dogwood-entrypoint: %s\n' "$*" >&2; }

stop_engine() {
    # Forwarders first, so nothing new arrives while the engine is going down.
    if [ "${#bridge_pids[@]}" -gt 0 ]; then
        for pid in "${bridge_pids[@]}"; do
            kill -TERM "$pid" 2>/dev/null || true
        done
    fi
    if [ -n "$engine_pid" ] && kill -0 "$engine_pid" 2>/dev/null; then
        # Best-effort clean checkpoint so a restart replays less of the log. Not
        # required for correctness — the log is durable and replay is what recovers it.
        dogwood-server --dir "$DW_DIR" checkpoint >/dev/null 2>&1 || true
        kill -TERM "$engine_pid" 2>/dev/null || true
        wait "$engine_pid" 2>/dev/null || true
    fi
}

# Expose one engine socket over TCP. See `bridge.py` for what the engine ends up
# authenticating (the forwarder, not the caller behind it).
start_bridge() {  # <bind> <unix socket> <label>
    python3 /usr/local/bin/dogwood-bridge --listen "$1" --unix "$2" --label "$3" &
    bridge_pids+=("$!")
}

shutdown() {
    trap - TERM INT
    stop_engine
}

start_engine() {
    mkdir -p "$DW_DIR"
    log "starting engine (store: $DW_DIR, snapshot every $SNAPSHOT_INTERVAL events)"
    dogwood-server --dir "$DW_DIR" run --snapshot-interval "$SNAPSHOT_INTERVAL" &
    engine_pid=$!

    local waited=0
    while ! dogwood-server --dir "$DW_DIR" ping >/dev/null 2>&1; do
        if ! kill -0 "$engine_pid" 2>/dev/null; then
            log "engine exited during startup"
            exit 1
        fi
        sleep 0.1
        waited=$((waited + 1))
        if [ "$waited" -gt 200 ]; then
            log "engine did not become ready within 20s"
            exit 1
        fi
    done
}

# Whether a *different* uid can reach the data socket, and where it is blocked if not.
#
# Always returns 0. The unreachable case is a *report*, not a failure: the engine is
# up and usable by its own uid, and the operator may legitimately want to run without
# the uid boundary. Letting the non-zero exit of the check propagate would trip
# `set -e` and kill the container right after explaining itself — which is exactly how
# a "forgot the third -v" turn into a silently dead container.
report_agent_reachability() {
    if socket-reachability.py "$AGENT_SOCKET" "$AGENT_UID" >/dev/null 2>&1; then
        log "agent side: uid $AGENT_UID can reach $AGENT_SOCKET"
        log "  e.g.  docker exec -u dogwood-agent <container> dogwood-entrypoint demo"
    else
        log "agent side: uid $AGENT_UID CANNOT reach $AGENT_SOCKET"
        # `|| true` is load-bearing: the checker exits 1 when it finds a blocker —
        # that is its job — and under `set -e -o pipefail` an unguarded non-zero here
        # would kill the container mid-explanation, which is exactly how "forgot the
        # third -v" would turn into a silently dead container instead of a message.
        socket-reachability.py "$AGENT_SOCKET" "$AGENT_UID" 2>&1 | sed 's/^/  /' >&2 || true
        log "  the engine's state directory is 0700 because it holds the policy set, and a"
        log "  socket below it is unreachable by another uid however the socket is chmodded."
        log "  To get the uid boundary, mount the socket volume a second time at a"
        log "  traversable path (see the docker run command in Dockerfile.socket-allinone),"
        log "  or just run the demo as the engine's uid with no boundary."
    fi
    return 0
}

install_argv() {
    local policy="${1:-${DOGWOOD_POLICY_FILE:-}}"
    local schema="${2:-${DOGWOOD_ACTION_SCHEMA_FILE:-}}"
    if [ -z "$policy" ] || [ -z "$schema" ]; then
        log "install needs a policy and an action schema: pass them as arguments, or set"
        log "DOGWOOD_POLICY_FILE and DOGWOOD_ACTION_SCHEMA_FILE."
        exit 64
    fi
    local args=(python3 "$SDK/examples/install_policy.py" --policy "$policy" --schema "$schema")
    if [ -n "${DOGWOOD_EVENT_SCHEMA_FILE:-}" ]; then
        args+=(--event-schema "$DOGWOOD_EVENT_SCHEMA_FILE")
    fi
    exec "${args[@]}"
}

cmd_run() {
    trap shutdown TERM INT
    start_engine

    if [ -n "${DOGWOOD_POLICY_FILE:-}" ]; then
        if [ -z "${DOGWOOD_ACTION_SCHEMA_FILE:-}" ]; then
            log "DOGWOOD_POLICY_FILE is set but DOGWOOD_ACTION_SCHEMA_FILE is not"
            exit 64
        fi
        log "installing policy $DOGWOOD_POLICY_FILE"
        local args=(policy install "$DOGWOOD_POLICY_FILE" --action-schema "$DOGWOOD_ACTION_SCHEMA_FILE")
        if [ -n "${DOGWOOD_EVENT_SCHEMA_FILE:-}" ]; then
            args+=(--event-schema "$DOGWOOD_EVENT_SCHEMA_FILE")
        fi
        dogwood-server --dir "$DW_DIR" "${args[@]}"
    fi

    log "the API is two Unix sockets."
    log "  data socket    (agent)   $DOGWOOD_SOCKET"
    log "  control socket (operator) $DOGWOOD_CONTROL_SOCKET"
    report_agent_reachability

    if [ -n "$TCP_DATA_BIND" ]; then
        start_bridge "$TCP_DATA_BIND" "$DW_DIR/run/dogwood.sock" data
    fi
    if [ -n "$TCP_CONTROL_BIND" ]; then
        start_bridge "$TCP_CONTROL_BIND" "$DW_DIR/control.sock" control
    fi
    if [ -n "$TCP_DATA_BIND" ] || [ -n "$TCP_CONTROL_BIND" ]; then
        log "TCP 转发已开：引擎认证的是转发器本身（uid $(id -u)），不是 TCP 那头的人。"
        [ -n "$TCP_DATA_BIND" ] && log "  data    tcp://$TCP_DATA_BIND"
        [ -n "$TCP_CONTROL_BIND" ] && log "  control tcp://$TCP_CONTROL_BIND  ← 谁能连谁就能改策略"
    fi

    # Nothing else to supervise: the engine is the only child, so this waits on it and
    # `shutdown` forwards the signal that ends the container.
    set +e
    wait "$engine_pid"
    rc=$?
    set -e
    log "engine exited (status $rc)"
    exit "$rc"
}

cmd_install() {
    install_argv "$@"
}

cmd_demo() {
    if [ "$(id -u)" != "$AGENT_UID" ]; then
        log "note: running as uid $(id -u), not the agent uid $AGENT_UID — this exercises the"
        log "      data plane, but not the uid boundary. For the boundary:"
        log "      docker exec -u dogwood-agent <container> dogwood-entrypoint demo"
    fi
    local sock="$AGENT_SOCKET"
    if [ ! -e "$sock" ]; then
        # No second mount of the socket volume, so the traversable path does not
        # exist. Fall back to the engine-side path, which works for the engine's uid
        # and is what the operator uid would use anyway.
        sock="$DW_DIR/run/dogwood.sock"
        log "note: $AGENT_SOCKET does not exist; using $sock instead"
    fi
    log "using data socket $sock as uid $(id -u)"
    DOGWOOD_SOCKET="$sock" exec python3 "$SDK/examples/decide.py" "$@"
}

cmd_test() {
    cd "$SDK"
    exec env DOGWOOD_TEST_STORE_DIR="$DW_DIR" DOGWOOD_TEST_POLICY_DIR="$EXAMPLES" \
        python3 -m unittest discover -s tests -v
}

# One command that proves the image works, with no volumes, no external state, and no
# Compose: start a throwaway engine, install the demo policy, walk the story, exit.
cmd_self_test() {
    tmp="$(mktemp -d /tmp/dogwood-self-test-XXXXXX)"
    DW_DIR="$tmp"
    SNAPSHOT_INTERVAL=0
    export DOGWOOD_STORE_DIR="$tmp"
    export DOGWOOD_SOCKET="$tmp/run/dogwood.sock"
    export DOGWOOD_CONTROL_SOCKET="$tmp/control.sock"

    trap 'stop_engine; rm -rf "$tmp"' EXIT
    start_engine

    log "installing $EXAMPLES/demo_policy.dw"
    python3 "$SDK/examples/install_policy.py" \
        --policy "$EXAMPLES/demo_policy.dw" --schema "$EXAMPLES/demo_schema.cedarschema"

    set +e
    python3 "$SDK/examples/decide.py" "$@"
    rc=$?
    set -e

    # A throwaway directory cannot host the uid boundary: the engine makes <dir> 0700,
    # and the fix is a second mount, which a temp dir has no way to provide. Say so
    # rather than let the absence look like a pass.
    log "note: a temp directory cannot exercise the uid boundary (see 'run' + docker exec -u)"
    exit "$rc"
}

case "${1:-run}" in
    run) shift || true; cmd_run "$@" ;;
    install) shift; cmd_install "$@" ;;
    demo) shift; cmd_demo "$@" ;;
    test) shift || true; cmd_test "$@" ;;
    self-test) shift; cmd_self_test "$@" ;;
    help | --help | -h)
        sed -n '3,40p' "$0" | sed 's/^# \{0,1\}//'
        ;;
    *)
        log "unknown command: $1 (try: run, install, demo, test, self-test, help)"
        exit 64
        ;;
esac
