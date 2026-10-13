"""The wire framing: a 4-byte big-endian length, then that many bytes of JSON.

This is the whole transport, and it is transcribed from
``dogwood-server/src/protocol.rs`` rather than invented here:

* one request per frame, one response per frame;
* a hard 16 MiB cap (``MAX_FRAME``), so a hostile length header is a bounded
  rejection rather than a multi-gigabyte allocation;
* length-prefixed rather than newline-delimited, because event field values are
  arbitrary strings and trivially contain newlines.

The functions take a socket rather than owning one, so they can be tested against a
``socketpair`` with no server anywhere in sight.
"""

from __future__ import annotations

import json
import socket
import struct
from typing import Any, Mapping

from .errors import DogwoodProtocolError, DogwoodTimeoutError, DogwoodTransportError

#: Matches ``dogwood-server/src/protocol.rs::MAX_FRAME``.
MAX_FRAME = 16 * 1024 * 1024


def write_frame(
    sock: socket.socket, payload: Mapping[str, Any], *, max_frame: int = MAX_FRAME
) -> None:
    """Encode and send one frame."""
    body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    if len(body) > max_frame:
        raise DogwoodProtocolError(
            f"frame of {len(body)} bytes exceeds the {max_frame}-byte limit"
        )
    try:
        sock.sendall(struct.pack(">I", len(body)) + body)
    except TimeoutError as exc:
        raise DogwoodTimeoutError(f"timed out sending a frame: {exc}") from exc
    except OSError as exc:
        raise DogwoodTransportError(f"send failed: {exc}") from exc


def read_frame(sock: socket.socket, *, max_frame: int = MAX_FRAME) -> dict[str, Any]:
    """Read exactly one frame and decode it."""
    (length,) = struct.unpack(">I", _read_exact(sock, 4))
    if length > max_frame:
        # Rejected from the header alone: the declared length is never trusted
        # enough to allocate against.
        raise DogwoodProtocolError(
            f"frame of {length} bytes exceeds the {max_frame}-byte limit"
        )
    body = _read_exact(sock, length)
    try:
        decoded = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise DogwoodProtocolError(f"response was not JSON: {exc}") from exc
    if not isinstance(decoded, dict):
        raise DogwoodProtocolError(f"response was {type(decoded).__name__}, not an object")
    return decoded


def _read_exact(sock: socket.socket, want: int) -> bytes:
    """Fill `want` bytes, or explain why not.

    A short read is the interesting case: it means the peer died mid-frame, and the
    stream position is now unknown — which is why the caller must not try to carry
    on with the same connection.
    """
    buffer = bytearray()
    while len(buffer) < want:
        try:
            chunk = sock.recv(want - len(buffer))
        except TimeoutError as exc:
            raise DogwoodTimeoutError(
                f"no answer within the deadline ({len(buffer)}/{want} bytes read)"
            ) from exc
        except OSError as exc:
            raise DogwoodTransportError(f"receive failed: {exc}") from exc
        if not chunk:
            raise DogwoodTransportError(
                f"connection closed mid-frame ({len(buffer)}/{want} bytes read)"
            )
        buffer += chunk
    return bytes(buffer)
