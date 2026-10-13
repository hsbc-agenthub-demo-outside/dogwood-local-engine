"""Where the bytes go: a Unix socket on this machine, or HTTP to a remote adapter.

# Why there are exactly two transports and not three

The engine speaks one protocol, and it speaks it over a **Unix socket** — there is no
TCP listener anywhere in `dogwood-server`. So "connect to a remote Dogwood" can only
mean connecting to something that is *already* in front of the engine:

* **`unix://`** — the engine's own socket. Same machine. This is the transport that
  preserves the engine's authorization model: the control plane is a `0700` socket
  and its callers are attested by kernel peer credential, so "who may author policy"
  is answered by the OS.
* **`http://` / `https://`** — the `dogwood-http` adapter. Any machine. The adapter is
  what makes the engine reachable over a network, and it pays for that by replacing
  the kernel's uid attestation with a bearer token — a strictly weaker scheme, which
  is why the adapter's own documentation is loud about it.

* **`tcp://host:port`** — the same frame protocol, carried over TCP by a forwarder
  that sits next to the engine and relays to its Unix socket. This is what makes the
  engine's *own* protocol reachable from another machine, with the SDK speaking the
  socket protocol rather than HTTP.

  **Read this before using it.** A TCP forwarder connects to the engine as itself, so
  the engine attests the *forwarder*, not the caller behind it: every client on the
  far side becomes that one uid. On the **data** plane that changes little — the data
  socket is `0666` and unauthenticated to begin with — but on the **control** plane it
  is the whole authorization story, so a control listener is exposed deliberately and
  narrowly or not at all. The alternative that keeps a real OS identity is forwarding
  the socket over SSH (`ssh -L /local.sock:/remote.sock`), which is still `unix://`
  from this client's point of view.

# The invariant these transports maintain

Both return a reply in the **engine's own shape** — `{"status": "decision", …}`,
`{"status": "applied", …}` — so everything above this module is transport-agnostic and
the typed parsing in `types.py` is written once.

The HTTP side has to do a little work for that, because its bodies are not tagged: a
200 from `POST /v1/policy` is a bare `AppliedResponse` with no `status` field, while
the wire form is `{"status":"applied", …}`. The tag is implied by the route, so the
transport puts it back — `setdefault`, so a body that already carries one (the data
plane's `SubmitResponse`) is left alone.

The other translation is the verdict: HTTP says "denied" with a **403**, which is not
a transport failure at all. The transport returns that body to the caller unchanged;
`DogwoodClient` already knows what to do with `{"allowed": false}`.
"""

from __future__ import annotations

import abc
import json
import os
import socket
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from .errors import (
    DogwoodAuthError,
    DogwoodEngineError,
    DogwoodError,
    DogwoodProtocolError,
    DogwoodTimeoutError,
    DogwoodTransportError,
)
from .framing import read_frame, write_frame


class Transport(abc.ABC):
    """One way of getting a reply out of the engine."""

    @abc.abstractmethod
    def call(self, op: str, **args: Any) -> dict[str, Any]:
        """Perform one engine operation and return the reply in wire shape."""

    @abc.abstractmethod
    def describe(self) -> str:
        """Where this transport points, for logs and `repr`."""


class _FramedTransport(Transport):
    """The engine's own protocol — a length-prefixed JSON frame each way.

    Both the Unix socket and the TCP forwarder carry exactly these bytes; only the
    `connect()` differs, so the framing lives here once.
    """

    def __init__(self, *, timeout: float) -> None:
        self.timeout = timeout

    @abc.abstractmethod
    def _connect(self) -> socket.socket:
        """A connected stream, or a transport error explaining why not."""

    def call(self, op: str, **args: Any) -> dict[str, Any]:
        sock = self._connect()
        try:
            write_frame(sock, {"op": op, **args})
            return read_frame(sock)
        finally:
            sock.close()


class UnixTransport(_FramedTransport):
    """The engine's own socket. One connection per call — see `client.py`."""

    def __init__(self, path: str | os.PathLike[str], *, timeout: float) -> None:
        super().__init__(timeout=timeout)
        self.path = str(path)

    def describe(self) -> str:
        return self.path

    def _connect(self) -> socket.socket:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(self.timeout)
        try:
            sock.connect(self.path)
        except TimeoutError as exc:
            sock.close()
            raise DogwoodTimeoutError(
                f"connect {self.path}: timed out after {self.timeout}s"
            ) from exc
        except OSError as exc:
            sock.close()
            raise DogwoodTransportError(f"connect {self.path}: {exc}") from exc
        return sock


class TcpTransport(_FramedTransport):
    """The same frame protocol, over TCP, through a forwarder next to the engine.

    The SDK cannot tell the difference — that is the point. What the *engine* sees is
    the forwarder: see the warning in this module's header before putting a control
    listener on a network.
    """

    def __init__(self, host: str, port: int, *, timeout: float) -> None:
        super().__init__(timeout=timeout)
        self.host = host
        self.port = port

    def describe(self) -> str:
        return f"tcp://{self.host}:{self.port}"

    def _connect(self) -> socket.socket:
        try:
            return socket.create_connection((self.host, self.port), timeout=self.timeout)
        except TimeoutError as exc:
            raise DogwoodTimeoutError(
                f"connect {self.describe()}: timed out after {self.timeout}s"
            ) from exc
        except OSError as exc:
            raise DogwoodTransportError(f"connect {self.describe()}: {exc}") from exc


class HttpTransport(Transport):
    """The HTTP adapter, in front of the same engine, for everyone off this machine."""

    def __init__(self, base_url: str, *, timeout: float, token: str | None = None) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.token = token

    def describe(self) -> str:
        return self.base_url

    def call(self, op: str, **args: Any) -> dict[str, Any]:
        method, path, body, tag = _route(op, args)
        status, payload = self._request(method, path, body)

        if status == 403 and op != "submit":
            # 403 is the data plane's "denied". Anywhere else it means something is in
            # front of the adapter that this client does not understand, and treating
            # it as a body would silently look like a successful call.
            raise DogwoodEngineError(
                f"unexpected 403 from {path}: {payload.get('error', payload)!r}"
            )

        if tag is not None:
            payload.setdefault("status", tag)
        return payload

    def _request(
        self, method: str, path: str, body: dict[str, Any] | None
    ) -> tuple[int, dict[str, Any]]:
        url = f"{self.base_url}{path}"
        data = None if body is None else json.dumps(body).encode("utf-8")
        headers = {"Accept": "application/json"}
        if data is not None:
            headers["Content-Type"] = "application/json"
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"

        request = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                status, raw = response.status, response.read()
        except urllib.error.HTTPError as exc:
            # 4xx/5xx are answers, not transport failures: the adapter explains itself
            # in the body, and its contract (see dogwood-http/README.md) is that every
            # non-2xx means "no verdict".
            status, raw = exc.code, exc.read()
        except TimeoutError as exc:
            raise DogwoodTimeoutError(
                f"{method} {url}: no answer within {self.timeout}s"
            ) from exc
        except urllib.error.URLError as exc:
            raise DogwoodTransportError(f"{method} {url}: {exc.reason}") from exc

        try:
            payload = json.loads(raw) if raw else {}
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise DogwoodProtocolError(
                f"{method} {url} answered {status} with a body that is not JSON: {exc}"
            ) from exc
        if not isinstance(payload, dict):
            raise DogwoodProtocolError(f"{method} {url} answered a non-object body")

        if 200 <= status < 300 or status == 403:
            return status, payload

        detail = payload.get("error") or payload.get("message") or payload
        if status == 401:
            raise DogwoodAuthError(f"{url}: {detail}")
        if status in (502, 503):
            # The adapter could not reach the engine, or the engine answered with an
            # error. From here that is the same thing as an unreachable engine.
            raise DogwoodTransportError(f"{url}: {detail}")
        if status == 504:
            raise DogwoodTimeoutError(f"{url}: {detail}")
        # 400 / 404 / 413 and anything else: the adapter rejected the request, and its
        # message is usually the engine's own finding.
        raise DogwoodEngineError(f"{url}: {detail}")


def _route(op: str, args: dict[str, Any]) -> tuple[str, str, dict[str, Any] | None, str | None]:
    """Map an engine operation onto the adapter's REST shape.

    Returns `(method, path, json_body, status_tag_to_restore)`.
    """
    if op == "ping":
        return "GET", "/v1/ping", None, "pong"
    if op == "submit":
        # The one body that is already tagged: decision vs recorded is the data
        # plane's own distinction and it survives the hop.
        return "POST", "/v1/events", args["event"], None

    if op == "install":
        body = {
            "policy": args["policy"],
            "action_schema": args["action_schema"],
        }
        if args.get("event_schema") is not None:
            body["event_schema"] = args["event_schema"]
        return "POST", "/v1/policy", body, "applied"
    if op == "batch":
        return "POST", "/v1/policy/verbs", {"verbs": args["verbs"]}, "batched"
    if op == "list":
        query = {}
        if args.get("max_results") is not None:
            query["max_results"] = args["max_results"]
        if args.get("next_token") is not None:
            query["after"] = args["next_token"]
        suffix = f"?{urllib.parse.urlencode(query)}" if query else ""
        return "GET", f"/v1/policies{suffix}", None, "policy_list"
    if op == "status":
        return "GET", "/v1/status", None, "status"
    if op == "get_policy":
        return "GET", "/v1/policy", None, "policy"
    if op == "get_policy_by_id":
        quoted = urllib.parse.quote(args["id"], safe="")
        return "GET", f"/v1/policies/{quoted}", None, "policy"
    if op == "get_schema":
        return "GET", "/v1/schema", None, "schema"
    if op == "checkpoint":
        return "POST", "/v1/checkpoint", None, "checkpointed"

    raise DogwoodError(
        f"the HTTP transport does not implement {op!r}. The adapter exposes a fixed "
        f"route set; if the engine gained a verb, the adapter has to learn it first."
    )


def transport_for(
    spec: str | os.PathLike[str], *, timeout: float, token: str | None = None
) -> Transport:
    """Build a transport from a URL, or from a bare filesystem path.

    * `unix:///abs/path` or `/abs/path` → the engine's socket
    * `tcp://host:port` → the engine's frame protocol, relayed over TCP
    * `http://host:port`, `https://host` → the adapter
    """
    text = str(spec)
    if text.startswith(("http://", "https://")):
        return HttpTransport(text, timeout=timeout, token=token)
    if text.startswith("tcp://"):
        parsed = urllib.parse.urlsplit(text)
        if not parsed.hostname or parsed.port is None:
            raise DogwoodError(f"{text} needs a host and a port, e.g. tcp://127.0.0.1:9000")
        return TcpTransport(parsed.hostname, parsed.port, timeout=timeout)
    if text.startswith("unix://"):
        return UnixTransport(text[len("unix://") :], timeout=timeout)
    return UnixTransport(text, timeout=timeout)
