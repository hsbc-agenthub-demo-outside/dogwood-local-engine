#!/usr/bin/env bash
# 用**真的** dogwood-server 跑这个 demo，并顺带验证重启恢复。
#
#   ./test_with_real_engine.sh
#
# 它会：构建服务端 → 用一个临时的小 Rust 工具把策略装进 store → 起服务 →
# 跑 demo（--no-stub --no-install）→ 杀掉服务再起来，确认历史还在 → 收尾。
#
# 为什么要那个小工具而不是 `dogwood-server policy install`：控制面的对端认证只
# 实现了 Linux 的 SO_PEERCRED，非 Linux 一律 fail-closed 拒绝。库里没有这层，
# 所以用它装策略，服务端启动时会恢复。
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HERE/.." && pwd)"
SERVER="$REPO/target/release/dogwood-server"
HELPER_DIR="${DW_HELPER_DIR:-$(mktemp -d "${TMPDIR:-/tmp}/dw-install-XXXXXX")}"
STORE_DIR="${DW_STORE_DIR:-$(mktemp -d "${TMPDIR:-/tmp}/dw-store-XXXXXX")}"
DOGWOOD_REV="996d756de1013b7ae209a14f566a80375a59f2f0"

# 用 mktemp 时目录已经建好了，但 DW_STORE_DIR 是用户给的路径，可能还不存在。
# 引擎（redb）不会替你建目录，所以这里必须补上 —— 否则装策略会以
# "No such file or directory" 失败。
mkdir -p "$STORE_DIR"
mkdir -p "$HELPER_DIR/src"

if [ -f "$HOME/.cargo/env" ]; then . "$HOME/.cargo/env"; fi
command -v cargo >/dev/null 2>&1 || {
    echo "需要 Rust 工具链：curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh" >&2
    exit 1
}
command -v python3 >/dev/null 2>&1 || { echo "需要 python3" >&2; exit 1; }

if [ ! -x "$SERVER" ]; then
    echo "[0] 构建 dogwood-server（第一次会拉 git 依赖，几分钟）"
    (cd "$REPO" && cargo build --release -p dogwood-server)
fi

# ── 生成装策略的小工具。独立 crate：自带空的 [workspace] 表，免得被误认为
#    属于本仓库的 Cargo workspace。
cat >"$HELPER_DIR/Cargo.toml" <<EOF
[package]
name = "dw-install"
version = "0.1.0"
edition = "2021"

[workspace]

[dependencies]
dogwood-local-engine = { path = "$REPO/dogwood-local-engine" }
dogwood-language = { git = "https://github.com/dogwood-policy/dogwood", rev = "$DOGWOOD_REV" }
EOF

cat >"$HELPER_DIR/src/main.rs" <<'RUST'
//! 把策略装进 store。控制面在非 Linux 上不可用，所以这里直接用引擎库。
use dogwood_local_engine::DurableTemporalEngine;

fn main() {
    let dir = std::env::args().nth(1).expect("用法: dw-install <dir> <policy.dw> <schema.cedarschema>");
    let policy =
        std::fs::read_to_string(std::env::args().nth(2).expect("policy 路径")).expect("读策略");
    let schema =
        std::fs::read_to_string(std::env::args().nth(3).expect("schema 路径")).expect("读 schema");

    // store 是单写者的：redb 会给文件加锁，所以服务端还开着的时候这里必然失败。
    // 把那个错误翻译成一句能直接照做的话，比抛 panic 有用。
    let mut engine = match DurableTemporalEngine::open(format!("{dir}/dogwood.redb"), 1000) {
        Ok(engine) => engine,
        Err(e) => {
            let msg = e.to_string();
            eprintln!("    打不开 store：{msg}");
            if msg.contains("already open") || msg.contains("lock") {
                eprintln!("    → 这个 store 正被另一个进程占着（多半是 dogwood-server 还在跑）。");
                eprintln!("      先到那个终端按 Ctrl-C，或者执行：");
                eprintln!("        pkill -f 'target/release/dogwood-server'");
            }
            std::process::exit(2);
        }
    };
    match engine.install(&policy, &schema, None, None) {
        Ok(applied) => println!(
            "    安装成功：rule_count={} leaf_count={}（真引擎解析 + 类型检查通过）",
            applied.rule_count, applied.leaf_count
        ),
        Err(e) => {
            eprintln!("    安装被拒绝：{e}");
            std::process::exit(2);
        }
    }
}
RUST

echo "== 1/4 装策略进 ${STORE_DIR}"
# 注意：这里**不能**把 stderr 丢掉。真引擎的报错（策略语法、类型错误、
# store 打不开）就是在这里第一次出现的，藏起来只会让人以为"什么都没发生"。
if ! (cd "$HELPER_DIR" && cargo run --quiet -- "$STORE_DIR" "$HERE/demo_policy.dw" "$HERE/demo_schema.cedarschema"); then
    echo "装策略失败：上面是真引擎的报错。" >&2
    exit 1
fi

echo "== 2/4 起 dogwood-server"
"$SERVER" --dir "$STORE_DIR" run >"$STORE_DIR/server.log" 2>&1 &
SERVER_PID=$!
cleanup() { kill "$SERVER_PID" 2>/dev/null || true; }
trap cleanup EXIT

# 就绪判断必须**真的连上去**，不能只看 socket 文件在不在：
# 上一个进程被 kill 后 socket 文件会留在原地（服务端下次 bind 时才 unlink 它），
# 所以 `[ -S ... ]` 会立刻通过，而这时还没有任何进程在监听 —— 于是第一个请求
# 撞上 "Connection refused"。用自带的 ping 子命令探活才是准的。
wait_ready() {
    local i
    for i in $(seq 1 150); do
        if "$SERVER" --dir "$STORE_DIR" ping >/dev/null 2>&1; then return 0; fi
        sleep 0.2
    done
    return 1
}

wait_ready || {
    echo "服务没起来（等了 30 秒），server.log 尾部：" >&2
    tail -20 "$STORE_DIR/server.log" >&2
    exit 1
}

echo "== 3/4 跑 demo（真引擎，三幕剧情）"
DOGWOOD_SOCKET="$STORE_DIR/run/dogwood.sock" \
    DOGWOOD_CONTROL_SOCKET="$STORE_DIR/control.sock" \
    python3 "$HERE/run_demo.py" --no-stub --no-install --quiet

echo
echo "== 4/4 重启恢复：杀掉服务、再起来，历史应该还在"
kill "$SERVER_PID" 2>/dev/null || true
wait "$SERVER_PID" 2>/dev/null || true
"$SERVER" --dir "$STORE_DIR" run >>"$STORE_DIR/server.log" 2>&1 &
SERVER_PID=$!
wait_ready || {
    echo "重启后服务没起来，server.log 尾部：" >&2
    tail -20 "$STORE_DIR/server.log" >&2
    exit 1
}

DOGWOOD_SOCKET="$STORE_DIR/run/dogwood.sock" python3 - <<'PY'
import os
import sys

from dogwood_client import DogwoodClient, describe

expected = {500: True, 777: False}   # 500 有批准历史 → 应 ALLOW；777 没批准过 → 应 DENY
bad = []
with DogwoodClient(os.environ["DOGWOOD_SOCKET"]) as client:
    for amount, want in expected.items():
        response = client.submit(
            'AgentCore::Action::refund', "request",
            principal='AgentCore::User::"alice"',
            resource='AgentCore::Account::"main"',
            logged={"input": {"amount": amount}},
            context={"input": {"amount": amount}},
        )
        got = response.get("status") == "decision" and response.get("allowed") is True
        print(f"    refund({amount}) -> {describe(response)}")
        if got != want:
            bad.append(f"refund({amount}) 期望 {'ALLOW' if want else 'DENY'}，实得 {'ALLOW' if got else 'DENY'}")

# 断言，而不是打印了事：重启后历史没恢复的话，这里必须让脚本失败。
if bad:
    print("    ✗ 与预期不符：" + "；".join(bad))
    sys.exit(1)
print("    ✓ 500 仍 ALLOW（批准历史从快照/日志里恢复了），777 仍 DENY（本来就没批准过）")
PY

echo
echo "全部通过。store 留在 ${STORE_DIR}（含 server.log），可删。"
