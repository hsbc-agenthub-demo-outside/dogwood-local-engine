"""**离线替身**：在 Python 里复刻 demo 用到的那一小块协议，不依赖 Rust。

用途只有一个：让这个 demo 在没有编译 `dogwood-server` 的机器上也能跑通、也能
当测试替身。它**不是** Dogwood 的实现 —— 它不解析 ``.dw``、不做 lowering、不做
类型检查、没有持久化、没有崩溃恢复，语义是照着 demo 的策略硬编码的。

真要判断"我的策略写得对不对"，请把它换成真的 `dogwood-server`（见 README）。
替身刻意保留了真服务端的几个行为，好让客户端代码不用改：

* 长度前缀帧、16 MiB 上限；
* ``WireEvent`` 拒绝未知字段（``deny_unknown_fields``）；
* 未知 ``op`` 返回 ``{"status":"error"}``；
* 时间戳由服务端赋值，客户端传 ``ts`` 会报错；
* 每个事件在锁下串行化、时间戳严格递增。
"""

from __future__ import annotations

import json
import os
import socket
import struct
import threading
import time
from pathlib import Path

MAX_FRAME = 16 * 1024 * 1024
ALLOWED_EVENT_FIELDS = {"action", "kind", "principal", "resource", "logged", "context", "entities", "parents"}

# demo 策略的硬编码模型：
#   permit Approve                                     —— 审批本身总是允许
#   permit Refund, when formerly within 1h Approve::response{ amount: same, approved: true }
WINDOW_NANOS = 3600 * 1_000_000_000


class StubServer:
    """最小可用的替身服务；进程内起，也可以单独跑。"""

    def __init__(self, dir_path: str | os.PathLike[str]):
        self.dir = Path(dir_path)
        self.run_dir = self.dir / "run"
        self.data_socket = self.run_dir / "dogwood.sock"
        self.control_socket = self.dir / "control.sock"
        self._lock = threading.Lock()
        self._approvals: list[tuple[int, int, int]] = []   # (ts, amount, approved)
        self._last_ts = 0
        self._installed = False
        self._decision_kinds = ["request"]
        self._threads: list[threading.Thread] = []
        self._stop = threading.Event()

    # ---- 生命周期 ------------------------------------------------------

    def start(self) -> "StubServer":
        self.dir.mkdir(parents=True, exist_ok=True)
        self.run_dir.mkdir(exist_ok=True)
        os.chmod(self.dir, 0o700)
        os.chmod(self.run_dir, 0o755)
        for path, mode in ((self.data_socket, 0o666), (self.control_socket, 0o700)):
            if path.exists():
                path.unlink()
            srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            srv.bind(str(path))
            os.chmod(path, mode)
            srv.listen(8)
            target = self._serve_data if path == self.data_socket else self._serve_control
            t = threading.Thread(target=self._accept_loop, args=(srv, target), daemon=True)
            t.start()
            self._threads.append(t)
        return self

    def stop(self) -> None:
        self._stop.set()
        for path in (self.data_socket, self.control_socket):
            try:
                path.unlink()
            except FileNotFoundError:
                pass

    # ---- 服务循环 ------------------------------------------------------

    def _accept_loop(self, srv: socket.socket, handler) -> None:
        srv.settimeout(0.2)
        while not self._stop.is_set():
            try:
                conn, _ = srv.accept()
            except TimeoutError:
                continue
            except OSError:
                return
            threading.Thread(target=handler, args=(conn,), daemon=True).start()

    def _serve_data(self, conn: socket.socket) -> None:
        self._serve(conn, self._handle_data)

    def _serve_control(self, conn: socket.socket) -> None:
        self._serve(conn, self._handle_control)

    def _serve(self, conn: socket.socket, handler) -> None:
        with conn:
            while not self._stop.is_set():
                body = self._read_frame(conn)
                if body is None:
                    return
                self._write_frame(conn, handler(body))

    @staticmethod
    def _read_frame(conn: socket.socket) -> dict | None:
        header = b""
        while len(header) < 4:
            chunk = conn.recv(4 - len(header))
            if not chunk:
                return None
            header += chunk
        (length,) = struct.unpack(">I", header)
        if length > MAX_FRAME:
            return None
        body = b""
        while len(body) < length:
            chunk = conn.recv(length - len(body))
            if not chunk:
                return None
            body += chunk
        try:
            return json.loads(body)
        except json.JSONDecodeError:
            return None

    @staticmethod
    def _write_frame(conn: socket.socket, payload: dict) -> None:
        body = json.dumps(payload, separators=(",", ":")).encode()
        conn.sendall(struct.pack(">I", len(body)) + body)

    # ---- 数据面 --------------------------------------------------------

    def _handle_data(self, body: dict) -> dict:
        op = body.get("op")
        if op == "ping":
            return {"status": "pong", "version": "stub"}
        if op != "submit":
            return {"status": "error", "message": f"unknown data op: {op!r}"}

        event = body.get("event")
        if not isinstance(event, dict):
            return {"status": "error", "message": "missing event"}
        unknown = set(event) - ALLOWED_EVENT_FIELDS
        if unknown:
            # 真服务端在这里报错，客户端传 `ts` 会被明确拒绝而不是被悄悄忽略。
            return {"status": "error", "message": f"unknown field(s) in event: {sorted(unknown)}"}
        action, kind = event.get("action"), event.get("kind")
        if not action or not kind:
            return {"status": "error", "message": "event needs action and kind"}

        with self._lock:
            self._last_ts = max(int(time.time() * 1e9), self._last_ts + 1)
            ts = self._last_ts
            logged = event.get("logged") or {}
            input_args = (logged.get("input") if isinstance(logged, dict) else None) or {}
            amount = input_args.get("amount") if isinstance(input_args, dict) else None

            if kind in self._decision_kinds:
                allowed, reason = self._decide(action, amount, ts)
                return {
                    "status": "decision",
                    "recorded_at_nanos": ts,
                    "allowed": allowed,
                    "reason": reason,
                }

            output = (logged.get("output") if isinstance(logged, dict) else None) or {}
            if self._action_name(action) == "approve" and isinstance(output, dict):
                self._approvals.append((ts, int(amount or 0), 1 if output.get("approved") else 0))
            return {"status": "recorded", "recorded_at_nanos": ts}

    @staticmethod
    def _action_name(action: str) -> str:
        """`AgentCore::Action::"refund"` -> `refund`。"""
        return action.rsplit("::", 1)[-1].strip('"')

    def _decide(self, action: str, amount, ts: int) -> tuple[bool, list[str]]:
        """deny by default；permit 来自 demo 策略（已硬编码）。"""
        if not self._installed:
            return False, []
        name = self._action_name(action)
        if name == "approve":
            return True, ["pol_approve"]
        if name == "refund":
            cutoff = ts - WINDOW_NANOS
            for when, approved_amount, approved in reversed(self._approvals):
                if when < cutoff:
                    break
                if approved and approved_amount == int(amount or 0):
                    return True, ["pol_refund_needs_approval"]
            return False, []
        return False, []

    # ---- 控制面 --------------------------------------------------------

    def _handle_control(self, body: dict) -> dict:
        op = body.get("op")
        if op == "install":
            if not body.get("policy") or not body.get("action_schema"):
                return {"status": "error", "message": "install needs policy and action_schema"}
            with self._lock:
                self._installed = True
                self._approvals.clear()
            return {
                "status": "applied",
                "applied_at_nanos": int(time.time() * 1e9),
                "rule_count": 2,
                "leaf_count": 1,
                "leaves_retained": 0,
                "leaves_prospective": 1,
            }
        if op == "status":
            with self._lock:
                return {
                    "status": "status",
                    "rule_count": 2 if self._installed else 0,
                    "leaf_count": 1 if self._installed else 0,
                    "incremental_leaves": 1 if self._installed else 0,
                    "decision_kinds": list(self._decision_kinds),
                    "control_uids": [os.getuid()],
                }
        return {"status": "error", "message": f"unknown control op: {op!r}"}


if __name__ == "__main__":  # 单独跑：python3 dogwood_stub_server.py /tmp/dogwood-demo
    import sys

    where = sys.argv[1] if len(sys.argv) > 1 else "/tmp/dogwood-demo"
    server = StubServer(where).start()
    print(f"stub listening on {server.data_socket} and {server.control_socket}; Ctrl-C to stop")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        server.stop()
