"""Clients for the engine's two planes — on this machine, or remote.

# Two planes, two clients, and that separation is the API

The engine puts its data plane and its control plane on **separate sockets with
disjoint verb sets**: ``DataRequest`` has no variant that reads, replaces, or
disables a policy. So this module has two classes and neither can reach the other's
verbs — :class:`DogwoodClient` for the agent's side, :class:`DogwoodAdminClient` for
the operator's. Handing an admin client to the process being governed is handing it
the ability to rewrite the rules that judge it.

# Local or remote, and what changes

An endpoint is a URL or a path, and it decides the transport
(:mod:`dogwood_sdk.transport`):

===========================  ==========================================================
`/run/dogwood/dogwood.sock`  the engine's own Unix socket — same machine
`http://host:8080`           the `dogwood-http` adapter — any machine
===========================  ==========================================================

Everything above the transport is identical: the same methods, the same typed
replies, the same fail-closed rule. What changes is *who is accountable* — a Unix
socket call is attested by the kernel (a uid), an HTTP call by a bearer token. That is
strictly weaker, which is why the adapter exists rather than a TCP bridge over the
socket; see the transport module's header.

# One request per connection, and no hidden retries

The Unix transport opens a connection, sends one frame, reads one frame, and closes.
That costs a ``connect()`` per request — negligible next to the fsync the engine does
on every append — and buys two things:

* no reconnect bookkeeping (the engine reaps idle connections after 30s, so a
  long-lived client needs it);
* no possibility of a stale connection turning into a stale *verdict*.

There are deliberately no retries. The engine does not deduplicate, so retrying a
submit whose answer was lost can double-append a history event and change what a
``formerly`` / ``count_within`` rule sees afterwards. A :class:`~dogwood_sdk.errors.DogwoodTimeoutError`
is the caller's to resolve, not this module's to paper over.

# The fail-closed helper

:meth:`DogwoodClient.allows` exists because the correct failure handling is
unforgettable-but-easy-to-forget: any error means "do not proceed". Callers who want
the reasoning can use :meth:`DogwoodClient.decide` and the exception; callers who
just want the safe answer call ``allows`` and get ``False`` on every failure.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from .errors import (
    DogwoodEngineError,
    DogwoodError,
    DogwoodProtocolError,
)
from .transport import Transport, transport_for
from .types import (
    Applied,
    Batched,
    Decision,
    PolicyPage,
    PolicySummary,
    Recorded,
    Schemas,
    Status,
    SubmitResult,
)

_wire = logging.getLogger("dogwood_sdk.wire")

#: Set ``DOGWOOD_TRACE=1`` to print every frame verbatim to stderr. The first tool to
#: reach for when a reply looks wrong: the protocol is four lines, so seeing it
#: unmangles most confusions immediately.
if os.environ.get("DOGWOOD_TRACE", "").lower() in {"1", "true", "yes", "on"}:
    if not _wire.handlers:
        _handler = logging.StreamHandler()
        _handler.setFormatter(logging.Formatter("%(message)s"))
        _wire.addHandler(_handler)
        _wire.setLevel(logging.DEBUG)
        _wire.propagate = False

_failures = logging.getLogger("dogwood_sdk")


def data_socket(store_dir: str | os.PathLike[str]) -> Path:
    """The data socket under the engine's state directory.

    ``<dir>/run/dogwood.sock``, matching ``dogwood-server``'s own layout.
    """
    return Path(store_dir) / "run" / "dogwood.sock"


def control_socket(store_dir: str | os.PathLike[str]) -> Path:
    """The control socket under the engine's state directory (``<dir>/control.sock``)."""
    return Path(store_dir) / "control.sock"


def _resolve_endpoint(
    endpoint: str | os.PathLike[str] | None,
    store_dir: str | os.PathLike[str] | None,
    env_vars: tuple[str, ...],
    under: Callable[[str | os.PathLike[str]], Path],
) -> str:
    """Where the engine is: explicit argument, then store directory, then environment.

    The order puts the two forms a caller can state *precisely* ahead of the
    environment, which is a deployment-wide default and the easiest thing to end up
    pointed at the wrong engine by accident.

    An endpoint is either a URL (`http://…`, `unix://…`) or a bare filesystem path;
    `transport_for` decides which, and rejects the schemes that would quietly break
    the engine's authorization with an explanation rather than a connection error.
    """
    if endpoint is not None:
        return str(endpoint)
    if store_dir is not None:
        return str(under(store_dir))
    for env_var in env_vars:
        from_env = os.environ.get(env_var)
        if from_env:
            return from_env
    joined = " or ".join(env_vars)
    raise DogwoodError(f"no endpoint: pass one, pass store_dir=, or set {joined}")


class _Endpoint:
    """The mechanics both clients share: where, how long, and one call at a time.

    Everything specific to a *transport* — Unix socket framing, or HTTP against the
    adapter — lives in :mod:`dogwood_sdk.transport`. Both present replies in the
    engine's own shape, which is what lets every method below ignore the difference.
    """

    def __init__(self, transport: Transport) -> None:
        self.transport = transport

    def describe(self) -> str:
        """Where this client points, for logs."""
        return self.transport.describe()

    def call(self, request: Mapping[str, Any]) -> dict[str, Any]:
        """Send one request and return one decoded response.

        The request is `{"op": …}` — the engine's own vocabulary. Over HTTP the
        transport maps it onto the adapter's route for that op.
        """
        if _wire.handlers:
            _wire.debug("-> %s", request)
        op = request.get("op")
        if not isinstance(op, str):
            raise DogwoodProtocolError(f"a request needs a string 'op', got {request!r}")
        response = self.transport.call(op, **{k: v for k, v in request.items() if k != "op"})
        if _wire.handlers:
            _wire.debug("<- %s", response)
        return response

    def _reject_if_engine_error(self, response: Mapping[str, Any]) -> None:
        """Turn the engine's own error variant into an exception.

        Not a verdict. On the control plane it is usually the validation findings for
        a rejected bundle.
        """
        if response.get("status") == "error":
            message = response.get("message")
            raise DogwoodEngineError(message if isinstance(message, str) else str(response))

    def __repr__(self) -> str:
        return f"{type(self).__name__}(endpoint={self.describe()!r})"


class DogwoodClient(_Endpoint):
    """The data plane: submit events, get verdicts.

    This is the client the monitored process may hold. It cannot read, replace, or
    disable a policy — not by convention, but because the socket it talks to does not
    implement those verbs.
    """

    def __init__(
        self,
        endpoint: str | os.PathLike[str] | None = None,
        *,
        store_dir: str | os.PathLike[str] | None = None,
        token: str | None = None,
        timeout: float = 5.0,
    ) -> None:
        """Where to find the engine.

        `endpoint` is a URL or a filesystem path:

            DogwoodClient("/run/dogwood/dogwood.sock")              # this machine
            DogwoodClient("unix:///run/dogwood/dogwood.sock")       # same, explicit
            DogwoodClient("http://dogwood.internal:8080")           # remote, via the adapter
            DogwoodClient("https://dogwood.internal", token="…")    # remote + TLS + token

        A bare path (the first form) is what earlier versions took as `socket_path`, so
        existing callers keep working unchanged.

        `store_dir` derives the conventional socket path from the engine's state
        directory. `DOGWOOD_ENDPOINT` then `DOGWOOD_SOCKET` are the environment
        fallbacks; `DOGWOOD_DATA_TOKEN` supplies `token` when it is not passed.
        """
        endpoint = _resolve_endpoint(
            endpoint, store_dir, ("DOGWOOD_ENDPOINT", "DOGWOOD_SOCKET"), data_socket
        )
        super().__init__(
            transport_for(
                endpoint,
                timeout=timeout,
                token=token if token is not None else os.environ.get("DOGWOOD_DATA_TOKEN"),
            )
        )

    def ping(self) -> str:
        """Liveness and version. Raises on any failure; there is no "probably up"."""
        response = self.call({"op": "ping"})
        self._reject_if_engine_error(response)
        version = response.get("version")
        if response.get("status") != "pong" or not isinstance(version, str):
            raise DogwoodProtocolError(f"unexpected reply to ping: {response!r}")
        return version

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
    ) -> SubmitResult:
        """Submit one event and return whatever the installed schema says it deserves.

        ``logged`` is the **durable temporal record** future timepoints correlate
        against; ``context`` is the **request-only** fields the current decision
        reads. They are deliberately separate bags, and a field both need is sent in
        both.

        There is no ``timestamp`` parameter, and that is not an oversight: the store
        assigns the time at append, so a caller cannot backdate an event outside a
        window or reorder itself ahead of another caller. The assigned value comes
        back as ``recorded_at_nanos``.
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
        return self.submit_event(event)

    def submit_event(self, event: Mapping[str, Any]) -> SubmitResult:
        """Submit an event that is already shaped as a wire event.

        The escape hatch for fields this client does not name, and the way to get the
        engine's own error message for a malformed event: the engine rejects an
        unknown field (a misspelled name, or a ``ts`` it deliberately does not have)
        with a message naming it, rather than silently dropping it — which would
        otherwise become an event with no history, failing closed for a reason nobody
        could see.
        """
        response = self.call({"op": "submit", "event": dict(event)})
        self._reject_if_engine_error(response)
        status = response.get("status")
        if status == "decision":
            return Decision.from_wire(response)
        if status == "recorded":
            return Recorded.from_wire(response)
        raise DogwoodProtocolError(f"unexpected reply to submit: {response!r}")

    def decide(
        self,
        action: str,
        kind: str,
        **event: Any,
    ) -> Decision:
        """Submit a decision-kind event and insist on a verdict.

        Raises :class:`DogwoodError` if the engine answers with a bare acknowledgement
        instead — which means the installed schema does not treat ``kind`` as a
        decision point. Ask the control plane's ``status()`` for ``decision_kinds``
        rather than guessing; under the built-in schema it is ``request``.
        """
        result = self.submit(action, kind, **event)
        if isinstance(result, Decision):
            return result
        raise DogwoodError(
            f"{kind!r} is not a decision kind under the installed schema, so the engine "
            f"recorded the event instead of judging it ({result})"
        )

    def record(self, action: str, kind: str, **event: Any) -> Recorded:
        """Submit a history-kind event and insist on the acknowledgement.

        Not optional in practice: temporal rules correlate against these, so an event
        trail that reports requests but never their outcomes leaves every
        ``formerly`` / ``count_within`` window empty — which presents as "the policy
        is wrong", not as "the history is missing".
        """
        result = self.submit(action, kind, **event)
        if isinstance(result, Recorded):
            return result
        raise DogwoodError(
            f"{kind!r} is a decision kind under the installed schema, so the engine "
            f"judged it instead of only recording it ({result})"
        )

    def allows(self, action: str, kind: str, **event: Any) -> bool:
        """``True`` only if the engine said so. Every failure returns ``False``.

        The fail-closed convenience: a transport error, a timeout, a malformed reply,
        or an engine error all mean "do not proceed", and this is the one place that
        is written down once instead of at every call site. The failure is logged, so
        turning it into a deny does not turn it into silence.
        """
        try:
            return self.decide(action, kind, **event).allowed
        except DogwoodError as exc:
            _failures.warning("%s: %s -> refusing", action, exc)
            return False


class DogwoodAdminClient(_Endpoint):
    """The control plane: author policy.

    **Not for the agent.** This client speaks to the privileged socket, which the
    engine's own server authorizes by kernel peer credential: on a real deployment
    the only uid that can reach it is the one that owns the engine process. Reaching
    it therefore means you are already the operator.
    """

    def __init__(
        self,
        endpoint: str | os.PathLike[str] | None = None,
        *,
        store_dir: str | os.PathLike[str] | None = None,
        token: str | None = None,
        # Longer than the data plane's default: an install revalidates and re-lowers
        # the whole policy set, which is a compile rather than a lookup.
        timeout: float = 30.0,
    ) -> None:
        """Where to find the engine's control plane.

            DogwoodAdminClient("/var/lib/dogwood/control.sock")          # this machine
            DogwoodAdminClient("http://dogwood.internal:8081", token=…)  # remote

        Over HTTP the token is required, and it is the same secret the adapter was
        started with; `DOGWOOD_CONTROL_TOKEN` is read when `token` is not passed. Over
        a Unix socket there is no token — the kernel attests the caller, which is a
        stronger guarantee than any secret this client could carry.
        """
        endpoint = _resolve_endpoint(
            endpoint,
            store_dir,
            ("DOGWOOD_CONTROL_ENDPOINT", "DOGWOOD_CONTROL_SOCKET"),
            control_socket,
        )
        super().__init__(
            transport_for(
                endpoint,
                timeout=timeout,
                token=token if token is not None else os.environ.get("DOGWOOD_CONTROL_TOKEN"),
            )
        )

    # ── the declarative path ─────────────────────────────────────────

    def install(
        self,
        policy: str,
        action_schema: str,
        event_schema: str | None = None,
    ) -> Applied:
        """Install a complete policy set plus its schemas, replacing what is running.

        Also the store-configuration entry point: it fixes the event schema, and a
        later install that changes it is rejected. Every policy is reborn, so existing
        history is discarded — use :meth:`add` / :meth:`batch` to keep it.

        Validated before accepted and swapped atomically, so a rejected bundle leaves
        the running set serving and this call is safe to repeat with a corrected one.
        """
        request: dict[str, Any] = {
            "op": "install",
            "policy": policy,
            "action_schema": action_schema,
        }
        if event_schema is not None:
            request["event_schema"] = event_schema
        return Applied.from_wire(self._control(request, expect="applied"))

    def install_files(
        self,
        policy: str | os.PathLike[str],
        action_schema: str | os.PathLike[str],
        event_schema: str | os.PathLike[str] | None = None,
    ) -> Applied:
        """Install from ``.dw`` / ``.cedarschema`` / ``.dwschema`` files."""
        return self.install(
            Path(policy).read_text(),
            Path(action_schema).read_text(),
            Path(event_schema).read_text() if event_schema is not None else None,
        )

    # ── the incremental path ─────────────────────────────────────────

    def add(self, policy: str) -> str:
        """Add one policy, born fresh. Returns the id the engine minted for it."""
        return self.batch([{"verb": "add", "policy": policy}]).minted[0]

    def update(self, policy_id: str, policy: str) -> Batched:
        """Replace one policy's content by id. Same id, but its history resets."""
        return self.batch([{"verb": "update", "id": policy_id, "policy": policy}])

    def delete(self, policy_id: str) -> Batched:
        """Remove one policy by id."""
        return self.batch([{"verb": "delete", "id": policy_id}])

    def reset(self, policy_id: str) -> Batched:
        """Clear one policy's accumulated history; its content is unchanged."""
        return self.batch([{"verb": "reset", "id": policy_id}])

    def delete_all(self) -> Batched:
        """Remove every policy. The schema is kept and the empty set stays installed,
        so decisions then fail closed."""
        return self.batch([{"verb": "delete_all"}])

    def reset_all(self) -> Batched:
        """Clear every policy's history; all policies are kept."""
        return self.batch([{"verb": "reset_all"}])

    def set_action_schema(self, action_schema: str) -> Batched:
        """Revalidate and re-lower the whole set under a new action schema."""
        return self.batch([{"verb": "set_action_schema", "action_schema": action_schema}])

    def append_action_schema(self, fragment: str) -> Batched:
        """Append a fragment onto the current action schema, atomically — so a caller
        can add an entity or action without a fetch-then-set round trip a concurrent
        change could invalidate."""
        return self.batch([{"verb": "append_action_schema", "fragment": fragment}])

    def batch(self, verbs: Sequence[Mapping[str, Any]]) -> Batched:
        """Apply several verbs atomically.

        All of them take effect together, or the whole batch is rejected and the
        running set is untouched. Prefer this over a sequence of single-verb calls
        whenever the intermediate state would be wrong: a ``delete`` followed by an
        ``add`` leaves a window in which the rule does not exist, and a decision
        landing in that window gets the wrong answer.
        """
        return Batched.from_wire(
            self._control({"op": "batch", "verbs": [dict(verb) for verb in verbs]}, expect="batched")
        )

    # ── reading ──────────────────────────────────────────────────────

    def list_policies(self, *, max_results: int | None = None, after: str | None = None) -> PolicyPage:
        """One page of policy metadata, most-stable-id first.

        Pagination is best-effort under concurrent mutation: if the policy named by
        ``after`` is deleted between pages, the walk resumes at the end and the
        remaining tail is skipped. Re-list from the top if that matters.
        """
        request: dict[str, Any] = {"op": "list"}
        if max_results is not None:
            request["max_results"] = max_results
        if after is not None:
            request["next_token"] = after
        return PolicyPage.from_wire(self._control(request, expect="policy_list"))

    def all_policies(self) -> tuple[PolicySummary, ...]:
        """Every policy's metadata, following the pagination for you."""
        collected: list[PolicySummary] = []
        token: str | None = None
        while True:
            page = self.list_policies(after=token)
            collected.extend(page.policies)
            if page.next_token is None:
                return tuple(collected)
            token = page.next_token

    def get_policy(self, policy_id: str) -> str:
        """One policy's canonical statement by id."""
        return self._policy_source({"op": "get_policy_by_id", "id": policy_id})

    def show_policy(self) -> str:
        """The whole installed policy source, every policy concatenated."""
        return self._policy_source({"op": "get_policy"})

    def schema(self) -> Schemas:
        """The store's action schema (as the operator authored it) and event schema."""
        return Schemas.from_wire(self._control({"op": "get_schema"}, expect="schema"))

    def status(self) -> Status:
        """What is installed and running.

        Read ``decision_kinds`` from here rather than hardcoding ``"request"``.
        """
        return Status.from_wire(self._control({"op": "status"}, expect="status"))

    def checkpoint(self) -> int:
        """Force a state snapshot now; returns the offset it covered."""
        response = self._control({"op": "checkpoint"}, expect="checkpointed")
        offset = response.get("up_to_offset")
        if not isinstance(offset, int):
            raise DogwoodProtocolError(f"checkpoint reply carried no offset: {response!r}")
        return offset

    # ── plumbing ─────────────────────────────────────────────────────

    def _control(self, request: Mapping[str, Any], *, expect: str) -> dict[str, Any]:
        response = self.call(request)
        self._reject_if_engine_error(response)
        if response.get("status") != expect:
            raise DogwoodProtocolError(
                f"expected a {expect!r} reply, got {response.get('status')!r}"
            )
        return response

    def _policy_source(self, request: Mapping[str, Any]) -> str:
        response = self._control(request, expect="policy")
        source = response.get("source")
        if not isinstance(source, str):
            raise DogwoodProtocolError("policy reply carried no source")
        return source
