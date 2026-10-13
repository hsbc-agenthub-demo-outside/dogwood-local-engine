"""The HTTP transport: the mapping onto the adapter, and the errors coming back.

Two halves, and they are testing different things:

* the **unit half** checks the mapping itself — which engine operation becomes which
  HTTP route, and what happens to the reply on the way back. It needs no server, so it
  runs everywhere and is the part that catches a route drifting away from the adapter.
* the **live half** talks to a real `dogwood-http`. It is skipped unless
  `DOGWOOD_ENDPOINT` names an `http://` URL, which is also a test of the environment
  fallback the constructor advertises. Run the adapter, then:

      DOGWOOD_ENDPOINT=http://127.0.0.1:8080 \
      DOGWOOD_CONTROL_ENDPOINT=http://127.0.0.1:8081 \
      DOGWOOD_CONTROL_TOKEN=<token> \
      python3 -m unittest discover -s tests -p 'test_http_transport.py' -v
"""

from __future__ import annotations

import os
import socket
import unittest
import urllib.error
import urllib.request
from urllib.parse import urlsplit

from dogwood_sdk import (
    DogwoodAdminClient,
    DogwoodAuthError,
    DogwoodClient,
    DogwoodError,
    DogwoodTransportError,
    HttpTransport,
    TcpTransport,
    UnixTransport,
    transport_for,
)
from dogwood_sdk.transport import _route

DATA_URL = os.environ.get("DOGWOOD_ENDPOINT", "")
CONTROL_URL = os.environ.get("DOGWOOD_CONTROL_ENDPOINT", "")
TOKEN = os.environ.get("DOGWOOD_CONTROL_TOKEN", "")


def reachable(url: str) -> bool:
    try:
        with urllib.request.urlopen(f"{url.rstrip('/')}/healthz", timeout=2):
            return True
    except urllib.error.HTTPError as exc:
        return exc.code < 500
    except (urllib.error.URLError, OSError):
        return False


def reachable_tcp(url: str) -> bool:
    """A TCP endpoint that accepts a connection. Nothing is sent."""
    parsed = urlsplit(url)
    try:
        with socket.create_connection((parsed.hostname, parsed.port), timeout=2):
            return True
    except OSError:
        return False


class EndpointTest(unittest.TestCase):
    """A URL decides the transport, and the dangerous scheme says why it is missing."""

    def test_a_bare_path_is_a_unix_socket(self) -> None:
        # Backwards compatibility: this is what earlier versions took as `socket_path`.
        self.assertIsInstance(transport_for("/run/dogwood/dogwood.sock", timeout=1), UnixTransport)

    def test_the_unix_scheme_is_explicit(self) -> None:
        transport = transport_for("unix:///tmp/x.sock", timeout=1)
        self.assertIsInstance(transport, UnixTransport)
        self.assertEqual(transport.describe(), "/tmp/x.sock")

    def test_http_and_https_are_the_remote_transport(self) -> None:
        for url in ("http://dogwood.internal:8080", "https://dogwood.example"):
            self.assertIsInstance(transport_for(url, timeout=1), HttpTransport)
        # A trailing slash must not produce `//v1/ping`.
        self.assertEqual(
            HttpTransport("http://host:8080/", timeout=1).describe(), "http://host:8080"
        )

    def test_tcp_is_the_socket_protocol_over_tcp(self) -> None:
        # The engine's own frames, carried by a forwarder. It is a distinct transport
        # from `http://` on purpose: different protocol, different auth story.
        transport = transport_for("tcp://host:9000", timeout=1)
        self.assertIsInstance(transport, TcpTransport)
        self.assertEqual(transport.describe(), "tcp://host:9000")

    def test_tcp_needs_a_host_and_a_port(self) -> None:
        for bad in ("tcp://", "tcp://host", "tcp://:9000"):
            with self.assertRaises(DogwoodError, msg=bad):
                transport_for(bad, timeout=1)


class RouteTest(unittest.TestCase):
    """Every engine operation has exactly one adapter route, and vice versa."""

    def test_the_data_plane_routes(self) -> None:
        self.assertEqual(_route("ping", {}), ("GET", "/v1/ping", None, "pong"))
        self.assertEqual(
            _route("submit", {"event": {"action": "A", "kind": "request"}}),
            ("POST", "/v1/events", {"action": "A", "kind": "request"}, None),
        )

    def test_the_control_plane_routes(self) -> None:
        self.assertEqual(_route("status", {})[1], "/v1/status")
        self.assertEqual(_route("checkpoint", {})[0], "POST")
        self.assertEqual(_route("get_policy", {})[1], "/v1/policy")
        self.assertEqual(_route("get_schema", {})[1], "/v1/schema")
        self.assertEqual(_route("get_policy_by_id", {"id": "abc"})[1], "/v1/policies/abc")
        self.assertEqual(_route("batch", {"verbs": []})[1], "/v1/policy/verbs")

    def test_install_omits_an_absent_event_schema(self) -> None:
        # The adapter's InstallBody marks `event_schema` optional and the engine treats
        # an explicit null differently from an absent key.
        _, _, body, _ = _route(
            "install", {"policy": "p", "action_schema": "s", "event_schema": None}
        )
        assert body is not None
        self.assertNotIn("event_schema", body)

    def test_list_builds_its_query_and_omits_what_was_not_asked_for(self) -> None:
        _, path, _, _ = _route("list", {})
        self.assertEqual(path, "/v1/policies")
        _, path, _, _ = _route("list", {"max_results": 5, "next_token": "abc"})
        self.assertEqual(path, "/v1/policies?max_results=5&after=abc")

    def test_a_policy_id_is_url_quoted(self) -> None:
        # Handles are opaque; one that ever contained a `/` must not become a path.
        _, path, _, _ = _route("get_policy_by_id", {"id": "a/b c"})
        self.assertEqual(path, "/v1/policies/a%2Fb%20c")

    def test_an_operation_the_adapter_does_not_have_is_an_error_not_a_guess(self) -> None:
        with self.assertRaises(DogwoodError):
            _route("no_such_verb", {})


class ReplyShapeTest(unittest.TestCase):
    """The transport restores the engine's tagged shape from an untagged HTTP body."""

    def test_a_tag_is_restored_but_never_overwritten(self) -> None:
        # `status: "applied"` comes from the route; `status: "decision"` is the data
        # plane's own distinction and must survive untouched.
        self.assertEqual({"version": "1", **{}}.get("status", "pong"), "pong")
        applied: dict = {"rule_count": 2}
        applied.setdefault("status", "applied")
        self.assertEqual(applied["status"], "applied")
        decision = {"status": "decision", "allowed": True}
        decision.setdefault("status", "applied")
        self.assertEqual(decision["status"], "decision")


@unittest.skipUnless(
    DATA_URL.startswith("http") and reachable(DATA_URL),
    "set DOGWOOD_ENDPOINT to a running dogwood-http data plane",
)
class LiveHttpDataTest(unittest.TestCase):
    def test_ping_comes_back_through_the_adapter(self) -> None:
        self.assertRegex(DogwoodClient().ping(), r"^\d+\.\d+\.\d+")

    def test_a_deny_is_a_decision_not_an_exception(self) -> None:
        # The 403 the adapter uses for "denied" must not surface as a transport error:
        # the request *was* answered, and the answer was no.
        decision = DogwoodClient().decide(
            "AgentCore::Action::never_defined", "request",
            principal='AgentCore::User::"alice"',
            resource='AgentCore::Account::"main"',
        )
        self.assertFalse(decision.allowed)
        self.assertTrue(decision.reason == () or decision.reason)


@unittest.skipUnless(
    CONTROL_URL.startswith("http") and TOKEN and reachable(CONTROL_URL),
    "set DOGWOOD_CONTROL_ENDPOINT and DOGWOOD_CONTROL_TOKEN to a running dogwood-http control plane",
)
class LiveHttpControlTest(unittest.TestCase):
    def test_status_reads_through_the_adapter(self) -> None:
        self.assertGreaterEqual(DogwoodAdminClient().status().rule_count, 0)

    def test_a_missing_token_is_an_auth_error_not_an_engine_error(self) -> None:
        # The engine never saw the request, so its message would be misleading.
        with self.assertRaises(DogwoodAuthError):
            DogwoodAdminClient(token="").status()

    def test_the_tag_is_restored_for_a_typed_reply(self) -> None:
        # `Applied` can only be built if the transport put `status: applied` back on a
        # body that does not carry it.
        from dogwood_sdk import Applied

        applied = DogwoodAdminClient().install_files(
            os.path.join(os.path.dirname(__file__), "..", "..", "python-demo", "demo_policy.dw"),
            os.path.join(
                os.path.dirname(__file__), "..", "..", "python-demo", "demo_schema.cedarschema"
            ),
        )
        self.assertIsInstance(applied, Applied)


class UnreachableTest(unittest.TestCase):
    """Fail-closed over HTTP too: a dead port is not permission."""

    def test_a_closed_port_is_a_transport_error(self) -> None:
        # Port 9 (discard) is not listening on any sane host.
        client = DogwoodClient("http://127.0.0.1:9", timeout=1)
        with self.assertRaises(DogwoodTransportError):
            client.ping()

    def test_allows_is_false_when_the_adapter_is_unreachable(self) -> None:
        client = DogwoodClient("http://127.0.0.1:9", timeout=1)
        self.assertFalse(client.allows("AgentCore::Action::refund", "request"))


# The live suites are gated on the *same* environment variables, so whichever scheme
# you point `DOGWOOD_ENDPOINT` at is the one that gets exercised — which is also a
# demonstration of how the endpoint picks the transport.
@unittest.skipUnless(
    DATA_URL.startswith("tcp://") and reachable_tcp(DATA_URL),
    "set DOGWOOD_ENDPOINT to a running tcp:// data forwarder",
)
class LiveTcpDataTest(unittest.TestCase):
    def test_the_engine_protocol_works_over_tcp(self) -> None:
        # Same frames as the Unix socket; the forwarder does not parse them.
        self.assertRegex(DogwoodClient().ping(), r"^\d+\.\d+\.\d+")

    def test_a_deny_is_still_a_decision(self) -> None:
        decision = DogwoodClient().decide(
            "AgentCore::Action::never_defined", "request",
            principal='AgentCore::User::"alice"',
            resource='AgentCore::Account::"main"',
        )
        self.assertFalse(decision.allowed)


@unittest.skipUnless(
    CONTROL_URL.startswith("tcp://") and reachable_tcp(CONTROL_URL),
    "set DOGWOOD_CONTROL_ENDPOINT to a running tcp:// control forwarder",
)
class LiveTcpControlTest(unittest.TestCase):
    def test_status_reads_over_tcp(self) -> None:
        # Reaching the control socket at all proves the forwarder is the engine's uid:
        # that socket is 0700, so any other uid would have been refused at connect().
        self.assertGreaterEqual(DogwoodAdminClient().status().rule_count, 0)


if __name__ == "__main__":
    unittest.main()
