"""The SDK against a real ``dogwood-server``, over a real socket.

`test_framing` proves the bytes; this proves the *conversation* — that the verb
names, the reply tags, and the field names in this package are the ones the engine
actually implements. A fake far end could not do that, because it would only be as
right as the person who wrote it.

Two things are platform-dependent and are asserted rather than skipped:

* the **control plane** authorizes callers by kernel peer credential
  (``SO_PEERCRED``), which is Linux-only. Off Linux the engine refuses every control
  caller by design, so the test asserts *that* — the failure is the specified
  behaviour, and a suite that quietly skipped it would hide a change in it.
* everything else — framing, data-plane verdicts, the fail-closed path — runs
  everywhere.

Two ways to get an engine:

* **spawn one** — the default. The binary is located by ``DOGWOOD_SERVER_BIN``, then
  ``target/release``, then ``target/debug``, and run on a temporary store.
* **use one that is already running** — set ``DOGWOOD_TEST_STORE_DIR`` to its state
  directory. That is how the suite runs inside the socket deployment's container,
  where there is no cargo build of its own. The tests then share the container's
  engine and leave it alone on the way out.

If neither is available the module is skipped with a message, because a test that
silently passes when it could not run is worse than no test.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from dogwood_sdk import (
    DogwoodAdminClient,
    DogwoodClient,
    DogwoodEngineError,
    DogwoodError,
    DogwoodTransportError,
    Recorded,
    data_socket,
)

REPO_ROOT = Path(__file__).resolve().parents[2]


def find_engine() -> Path | None:
    explicit = os.environ.get("DOGWOOD_SERVER_BIN")
    if explicit:
        return Path(explicit) if Path(explicit).is_file() else None
    for profile in ("release", "debug"):
        candidate = REPO_ROOT / "target" / profile / "dogwood-server"
        if candidate.is_file():
            return candidate
    return None


ENGINE = find_engine()
EXTERNAL_STORE = os.environ.get("DOGWOOD_TEST_STORE_DIR")

#: Where the demo policy and schema live. The default suits a checkout; a container
#: that mounts them elsewhere says so, because `__file__` cannot tell it where the
#: repository went.
POLICY_DIR = Path(
    os.environ.get("DOGWOOD_TEST_POLICY_DIR") or (REPO_ROOT / "python-demo")
)


@unittest.skipUnless(
    ENGINE or EXTERNAL_STORE,
    "no engine: set DOGWOOD_TEST_STORE_DIR to a running one, or "
    "`cargo build -p dogwood-server` and optionally DOGWOOD_SERVER_BIN",
)
class LiveTest(unittest.TestCase):
    """One engine per test class, so the durable store is shared and ordered."""

    @classmethod
    def setUpClass(cls) -> None:
        if EXTERNAL_STORE:
            # Someone else's engine: do not touch its lifetime, only its API.
            cls.process = None
            cls.store_dir = Path(EXTERNAL_STORE)
            socket_path = data_socket(cls.store_dir)
            deadline = time.monotonic() + 10
            while not socket_path.exists():
                if time.monotonic() > deadline:
                    raise RuntimeError(f"no engine listening at {socket_path}")
                time.sleep(0.05)
            return

        assert ENGINE is not None
        cls.store_dir = Path(tempfile.mkdtemp(prefix="dogwood-sdk-"))
        cls.process = subprocess.Popen(
            [str(ENGINE), "--dir", str(cls.store_dir), "run"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        deadline = time.monotonic() + 10
        socket_path = data_socket(cls.store_dir)
        while not socket_path.exists():
            if time.monotonic() > deadline:
                cls.process.kill()
                raise RuntimeError(f"the engine never bound {socket_path}")
            time.sleep(0.02)

    @classmethod
    def tearDownClass(cls) -> None:
        if cls.process is None:
            return
        cls.process.terminate()
        try:
            cls.process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            cls.process.kill()
        shutil.rmtree(cls.store_dir, ignore_errors=True)

    # ── data plane ───────────────────────────────────────────────────

    def test_ping_reports_a_version(self) -> None:
        version = DogwoodClient(store_dir=self.store_dir).ping()
        self.assertRegex(version, r"^\d+\.\d+\.\d+")

    def test_an_unreachable_engine_refuses_rather_than_allows(self) -> None:
        # The whole point. A client that answers "yes" when it cannot reach the
        # engine has been defeated without the engine being consulted.
        orphan = DogwoodClient("/tmp/dogwood-definitely-not-running.sock", timeout=0.5)
        with self.assertRaises(DogwoodTransportError):
            orphan.decide("A::Action::X", "request")
        self.assertFalse(orphan.allows("A::Action::X", "request"))

    def test_an_unknown_event_field_is_reported_by_the_engine(self) -> None:
        # The engine, not this package, is the authority on what a valid event is —
        # and it names the offending field rather than dropping it.
        with self.assertRaises(DogwoodEngineError) as caught:
            DogwoodClient(store_dir=self.store_dir).submit_event(
                {"action": "A::Action::Read", "kind": "request", "ts": 1}
            )
        self.assertIn("ts", str(caught.exception))

    # ── the temporal story, end to end ───────────────────────────────

    def test_a_refund_needs_a_matching_approval(self) -> None:
        if sys.platform != "linux":
            self.skipTest("the control plane only authorizes callers on Linux")
        if not (POLICY_DIR / "demo_policy.dw").is_file():
            self.skipTest(f"no demo policy under {POLICY_DIR} (set DOGWOOD_TEST_POLICY_DIR)")

        admin = DogwoodAdminClient(store_dir=self.store_dir)
        admin.install_files(
            POLICY_DIR / "demo_policy.dw",
            POLICY_DIR / "demo_schema.cedarschema",
        )
        status = admin.status()
        self.assertEqual(status.rule_count, 2)
        self.assertIn("request", status.decision_kinds)

        amount = 4242
        client = DogwoodClient(store_dir=self.store_dir)
        refund = {
            "principal": 'AgentCore::User::"alice"',
            "resource": 'AgentCore::Account::"main"',
            "logged": {"input": {"amount": amount}},
            "context": {"input": {"amount": amount}},
        }

        # 1. No approval in the window → the engine denies.
        self.assertFalse(client.decide("AgentCore::Action::refund", "request", **refund).allowed)

        # 2. The approval's outcome is recorded. Not optional in practice: the rule
        #    correlates against these history events, and without them every window
        #    stays empty and the rule presents as simply broken.
        recorded = client.record(
            "AgentCore::Action::approve",
            "response",
            principal=refund["principal"],
            resource=refund["resource"],
            logged={"input": {"amount": amount}, "output": {"approved": True}},
            context={"input": {"amount": amount}},
        )
        self.assertIsInstance(recorded, Recorded)
        self.assertGreater(recorded.recorded_at_nanos, 0)

        # 3. The same refund, now covered, is allowed — and the reply names the rule.
        allowed = client.decide("AgentCore::Action::refund", "request", **refund)
        self.assertTrue(allowed.allowed, f"expected an allow, got {allowed}")
        self.assertTrue(allowed.reason, "an allow should name its determining policy")

        # 4. A different amount was never approved, so it is still denied — the
        #    window is keyed on the amount, which is what makes this a real
        #    correlation test rather than a "something was approved" test.
        other = {**refund, "context": {"input": {"amount": amount + 1}}}
        self.assertFalse(client.decide("AgentCore::Action::refund", "request", **other).allowed)

    def test_the_control_plane_refuses_off_linux(self) -> None:
        if sys.platform == "linux":
            self.skipTest("on Linux the control plane is expected to work")
        with self.assertRaises(DogwoodEngineError) as caught:
            DogwoodAdminClient(store_dir=self.store_dir).status()
        self.assertIn("peer attestation", str(caught.exception))

    # ── the SDK's own contract ───────────────────────────────────────

    def test_every_error_is_a_dogwood_error(self) -> None:
        # One `except DogwoodError` must be enough to be fail-closed.
        for exc in (DogwoodTransportError, DogwoodEngineError):
            self.assertTrue(issubclass(exc, DogwoodError))


if __name__ == "__main__":
    unittest.main()
