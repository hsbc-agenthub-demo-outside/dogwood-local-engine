"""Typed replies, parsed once at the boundary.

The wire says :class:`dict`; callers deserve better than
``response["allowed"] == True``, which is one typo away from
``response["allow"] is None`` quietly denying everything (or, worse, one
``not response.get("denied")`` away from allowing everything).

The parse is also where an unknown shape becomes an error rather than a default: a
reply that does not look like what the protocol says is a
:class:`~dogwood_sdk.errors.DogwoodProtocolError`, which is fail-closed, instead of a
`dict` whose missing key reads as falsy somewhere far away.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping

from .errors import DogwoodProtocolError


def _int(raw: Mapping[str, Any], key: str) -> int:
    value = raw.get(key)
    if not isinstance(value, int) or isinstance(value, bool):
        raise DogwoodProtocolError(f"expected an integer {key!r}, got {value!r}")
    return value


def _strings(raw: Mapping[str, Any], key: str) -> tuple[str, ...]:
    value = raw.get(key, [])
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise DogwoodProtocolError(f"expected {key!r} to be a list of strings, got {value!r}")
    return tuple(value)


# ─── Data plane ──────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class Decision:
    """A verdict on a decision-kind event.

    :attr:`allowed` is the only field that grants anything, and it is named for the
    question the caller is asking — so an unrecognized spelling cannot be mistaken
    for permission.
    """

    allowed: bool
    reason: tuple[str, ...] = ()
    errors: tuple[str, ...] = ()
    #: When the store recorded the event, in epoch **nanoseconds**. The verdict is
    #: only meaningful relative to a moment, and this is that moment: every window
    #: the rules declare is measured against it. The caller cannot derive it — the
    #: store assigns it — so it is the one thing a client both needs and cannot
    #: work out itself.
    recorded_at_nanos: int = 0

    @classmethod
    def from_wire(cls, raw: Mapping[str, Any]) -> Decision:
        allowed = raw.get("allowed")
        if not isinstance(allowed, bool):
            raise DogwoodProtocolError(f"decision carried a non-boolean 'allowed': {allowed!r}")
        return cls(
            allowed=allowed,
            reason=_strings(raw, "reason"),
            errors=_strings(raw, "errors"),
            recorded_at_nanos=_int(raw, "recorded_at_nanos"),
        )

    def __str__(self) -> str:
        verdict = "ALLOW" if self.allowed else "DENY"
        if self.allowed:
            return f"{verdict} at={self.recorded_at_nanos}ns"
        parts = [f"{verdict} at={self.recorded_at_nanos}ns"]
        if self.reason:
            parts.append(f"determined by {', '.join(self.reason)}")
        if self.errors:
            parts.append(f"errors: {'; '.join(self.errors)}")
        return "  ".join(parts)


@dataclass(frozen=True, slots=True)
class Recorded:
    """A history-kind event was durably recorded; no verdict applies.

    The *arrival* of this is what tells the caller the event is on disk — history
    events are not fire-and-forget.
    """

    recorded_at_nanos: int

    @classmethod
    def from_wire(cls, raw: Mapping[str, Any]) -> Recorded:
        return cls(recorded_at_nanos=_int(raw, "recorded_at_nanos"))

    def __str__(self) -> str:
        return f"recorded at={self.recorded_at_nanos}ns"


#: What a submit can come back with. Which one you get is the *installed schema's*
#: decision, not the client's: it decides which kinds are decision points.
SubmitResult = Decision | Recorded


# ─── Control plane ───────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class Applied:
    """A policy set landed."""

    applied_at_nanos: int
    rule_count: int
    leaf_count: int
    leaves_retained: int
    leaves_prospective: int

    @classmethod
    def from_wire(cls, raw: Mapping[str, Any]) -> Applied:
        return cls(
            applied_at_nanos=_int(raw, "applied_at_nanos"),
            rule_count=_int(raw, "rule_count"),
            leaf_count=_int(raw, "leaf_count"),
            leaves_retained=_int(raw, "leaves_retained"),
            leaves_prospective=_int(raw, "leaves_prospective"),
        )

    def __str__(self) -> str:
        text = f"{self.rule_count} rule(s), {self.leaf_count} temporal leaf/leaves"
        if self.leaves_prospective:
            # The warm-up consequence is the surprising half of a prospective
            # install, so it is stated where the operator sees the result.
            text += (
                f"; {self.leaves_prospective} start with an empty window, so a new or "
                "edited temporal rule does not fully bite until its window has elapsed"
            )
        return text


@dataclass(frozen=True, slots=True)
class Batched:
    """A batch of policy verbs landed."""

    minted: tuple[str, ...] = ()
    applied_at_nanos: int = 0

    @classmethod
    def from_wire(cls, raw: Mapping[str, Any]) -> Batched:
        return cls(
            minted=_strings(raw, "minted"),
            applied_at_nanos=_int(raw, "applied_at_nanos"),
        )

    def __str__(self) -> str:
        if not self.minted:
            return f"batch applied at={self.applied_at_nanos}ns"
        return f"batch applied at={self.applied_at_nanos}ns, minted {list(self.minted)}"


@dataclass(frozen=True, slots=True)
class PolicySummary:
    """A policy's metadata, without its source."""

    id: str
    created: int
    updated: int

    @classmethod
    def from_wire(cls, raw: Mapping[str, Any]) -> PolicySummary:
        policy_id = raw.get("id")
        if not isinstance(policy_id, str):
            raise DogwoodProtocolError(f"policy summary had no string 'id': {raw!r}")
        return cls(id=policy_id, created=_int(raw, "created"), updated=_int(raw, "updated"))

    def __str__(self) -> str:
        return f"{self.id}  created {self.created}  updated {self.updated}"


@dataclass(frozen=True, slots=True)
class PolicyPage:
    """A page of policy metadata. :attr:`next_token` resumes the walk."""

    policies: tuple[PolicySummary, ...] = ()
    next_token: str | None = None

    @classmethod
    def from_wire(cls, raw: Mapping[str, Any]) -> PolicyPage:
        listed = raw.get("policies", [])
        if not isinstance(listed, list):
            raise DogwoodProtocolError(f"expected 'policies' to be a list, got {listed!r}")
        token = raw.get("next_token")
        return cls(
            policies=tuple(PolicySummary.from_wire(item) for item in listed),
            next_token=token if isinstance(token, str) else None,
        )


@dataclass(frozen=True, slots=True)
class Schemas:
    """The store's configured schemas."""

    action_schema: str
    event_schema: str | None = None

    @classmethod
    def from_wire(cls, raw: Mapping[str, Any]) -> Schemas:
        action_schema = raw.get("action_schema")
        if not isinstance(action_schema, str):
            raise DogwoodProtocolError("no action schema in the reply")
        event_schema = raw.get("event_schema")
        return cls(
            action_schema=action_schema,
            event_schema=event_schema if isinstance(event_schema, str) else None,
        )


@dataclass(frozen=True, slots=True)
class Status:
    """What is installed and running."""

    rule_count: int
    leaf_count: int
    incremental_leaves: int
    #: The kinds the installed schema treats as decision points. Read this rather
    #: than hardcoding ``"request"``: which kinds decide is schema configuration.
    decision_kinds: tuple[str, ...] = field(default_factory=tuple)
    #: Empty means unshardable — the schema declares no universal symmetric pin.
    partition_key: tuple[str, ...] = field(default_factory=tuple)
    log_offset: int = 0
    control_uids: tuple[int, ...] = field(default_factory=tuple)

    @classmethod
    def from_wire(cls, raw: Mapping[str, Any]) -> Status:
        uids = raw.get("control_uids", [])
        if not isinstance(uids, list) or not all(isinstance(u, int) for u in uids):
            raise DogwoodProtocolError(f"expected 'control_uids' to be integers, got {uids!r}")
        return cls(
            rule_count=_int(raw, "rule_count"),
            leaf_count=_int(raw, "leaf_count"),
            incremental_leaves=_int(raw, "incremental_leaves"),
            decision_kinds=_strings(raw, "decision_kinds"),
            partition_key=_strings(raw, "partition_key"),
            log_offset=_int(raw, "log_offset"),
            control_uids=tuple(uids),
        )

    def is_empty(self) -> bool:
        """True when no policy set is installed — decisions then fail closed."""
        return self.rule_count == 0
