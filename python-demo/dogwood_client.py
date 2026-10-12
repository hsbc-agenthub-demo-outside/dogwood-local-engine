"""Dogwood 的 Python 客户端：**4 字节大端长度前缀 + JSON**，走 Unix socket。

协议不是这里发明的，它逐字对应 `dogwood-server` 的 `src/protocol.rs`：

* 一帧 = 4 字节大端长度 + 该长度的 JSON；
* 单帧上限 16 MiB（`MAX_FRAME`）；
* 数据面 ``<dir>/run/dogwood.sock`` 与 ``control.sock`` 是**两个** socket，
  数据面只有 ``submit`` / ``ping``，没有读或改策略的动词。

这一层只做两件事：把事件发过去，把裁决拿回来。它不拦截、不执行任何工具 ——
拦截永远是调用方的责任（见 ``dogwood_middleware.py``）。
"""

from __future__ import annotations

import json
import logging
import os
import socket
import struct
from contextlib import AbstractContextManager
from typing import Any, Mapping

#: 与 `dogwood-server/src/protocol.rs::MAX_FRAME` 一致。
MAX_FRAME = 16 * 1024 * 1024

#: 默认事件 schema 下的两种 kind：决策点拿裁决，历史事件只入账。
DECISION_KIND = "request"
HISTORY_KIND = "response"

#: ``DOGWOOD_TRACE=1`` 时把每一帧原样打到 stderr —— 调协议时的第一件工具。
_TRACE = os.environ.get("DOGWOOD_TRACE", "").lower() in {"1", "true", "yes", "on"}
_wire = logging.getLogger("dogwood.wire")
if _TRACE and not _wire.handlers:
    _handler = logging.StreamHandler()          # 默认就是 stderr
    _handler.setFormatter(logging.Formatter("%(message)s"))
    _wire.addHandler(_handler)
    _wire.setLevel(logging.DEBUG)
    _wire.propagate = False


class DogwoodError(RuntimeError):
    """传输或协议层错误。调用方必须 **fail closed**：当作拒绝处理。"""


def _write_frame(sock: socket.socket, payload: Mapping[str, Any]) -> None:
    body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    if len(body) > MAX_FRAME:
        raise DogwoodError(f"frame too large: {len(body)} bytes (max {MAX_FRAME})")
    sock.sendall(struct.pack(">I", len(body)) + body)


def _read_exact(sock: socket.socket, want: int) -> bytes:
    buf = bytearray()
    while len(buf) < want:
        chunk = sock.recv(want - len(buf))
        if not chunk:
            raise DogwoodError("connection closed mid-frame")
        buf += chunk
    return bytes(buf)


def _read_frame(sock: socket.socket) -> dict[str, Any]:
    (length,) = struct.unpack(">I", _read_exact(sock, 4))
    if length > MAX_FRAME:
        raise DogwoodError(f"frame too large: {length} bytes (max {MAX_FRAME})")
    return json.loads(_read_exact(sock, length))


class DogwoodClient(AbstractContextManager["DogwoodClient"]):
    """一条到 ``dogwood-server`` 的长连接。

    服务端会给空闲连接设 30 秒读超时（`DEFAULT_IDLE_TIMEOUT`），所以长期挂着的
    客户端要能重连；本类把重连交给调用方，不偷偷吞掉传输错误。
    """

    def __init__(self, socket_path: str | os.PathLike[str], *, timeout: float = 5.0):
        self.socket_path = str(socket_path)
        self._sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._sock.settimeout(timeout)
        try:
            self._sock.connect(self.socket_path)
        except OSError as exc:
            self._sock.close()
            raise DogwoodError(f"connect {self.socket_path}: {exc}") from exc
        self._sock.settimeout(None)

    # ---- 传输 ----------------------------------------------------------

    def call(self, request: Mapping[str, Any]) -> dict[str, Any]:
        """发一帧、收一帧。一个连接可以连发多帧（服务端循环读）。"""
        try:
            _write_frame(self._sock, request)
            if _TRACE:
                _wire.debug("-> %s", json.dumps(request, ensure_ascii=False))
            response = _read_frame(self._sock)
            if _TRACE:
                _wire.debug("<- %s", json.dumps(response, ensure_ascii=False))
            return response
        except OSError as exc:
            raise DogwoodError(f"transport: {exc}") from exc

    def close(self) -> None:
        self._sock.close()

    def __exit__(self, *exc: object) -> None:
        self.close()

    # ---- 数据面 --------------------------------------------------------

    def ping(self) -> dict[str, Any]:
        return self.call({"op": "ping"})

    def submit(
        self,
        action: str,
        kind: str,
        *,
        principal: str | None = None,
        resource: str | None = None,
        logged: Mapping[str, Any] | None = None,
        context: Mapping[str, Any] | None = None,
        entities: Mapping[str, Any] | None = None,
        parents: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """提交一个事件。

        ``logged`` 是**未来**时间点要匹配的历史字段，``context`` 是**当前**决策
        读到的请求级字段 —— 两个袋子分开，同一次调用的参数通常两袋都填。
        这里刻意不提供时间戳：`WireEvent` 没有该字段，服务端在 append 时赋值。
        """
        event: dict[str, Any] = {"action": action, "kind": kind}
        if principal is not None:
            event["principal"] = principal
        if resource is not None:
            event["resource"] = resource
        if logged:
            event["logged"] = dict(logged)
        if context:
            event["context"] = dict(context)
        if entities:
            event["entities"] = dict(entities)
        if parents:
            event["parents"] = dict(parents)
        return self.call({"op": "submit", "event": event})


class ControlClient(DogwoodClient):
    """控制面客户端：只有它能碰策略集（服务端按 uid 白名单授权）。

    这个类**不是**给 agent 用的。让被治理的进程拿到它，等于让它改审判自己的规则。
    """

    def install(
        self,
        policy: str,
        action_schema: str,
        event_schema: str | None = None,
    ) -> dict[str, Any]:
        req: dict[str, Any] = {
            "op": "install",
            "policy": policy,
            "action_schema": action_schema,
        }
        if event_schema is not None:
            req["event_schema"] = event_schema
        return self.call(req)

    def status(self) -> dict[str, Any]:
        return self.call({"op": "status"})


# ---- 裁决的读法：只有一种"通过" --------------------------------------------


def is_allowed(response: Mapping[str, Any]) -> bool:
    """**唯一的**通过条件：``status == "decision"`` 且 ``allowed is True``。

    传输错误、``{"status":"error"}``、字段缺失、类型不对 —— 全部不是通过。
    写成 ``if response != "deny": run()`` 就是在给策略开后门。
    """
    return response.get("status") == "decision" and response.get("allowed") is True


def describe(response: Mapping[str, Any]) -> str:
    """把一帧响应压成一行日志（不用于判断，只用于展示）。"""
    status = response.get("status")
    if status == "decision":
        verdict = "ALLOW" if response.get("allowed") is True else "DENY"
        reason = response.get("reason") or []
        return f"decision={verdict} at={response.get('recorded_at_nanos')} reason={reason}"
    if status == "recorded":
        return f"recorded at={response.get('recorded_at_nanos')}"
    return f"{status}: {response.get('message', response)}"


def client_from_env() -> DogwoodClient:
    """``DOGWOOD_SOCKET`` 指向数据面 socket；默认用本机 demo 的临时路径。"""
    path = os.environ.get("DOGWOOD_SOCKET", "/tmp/dogwood-demo/run/dogwood.sock")
    return DogwoodClient(path)


def control_from_env() -> ControlClient:
    path = os.environ.get("DOGWOOD_CONTROL_SOCKET", "/tmp/dogwood-demo/control.sock")
    return ControlClient(path)
