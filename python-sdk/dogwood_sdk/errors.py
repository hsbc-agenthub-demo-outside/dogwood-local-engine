"""The failure taxonomy, and the one rule that governs all of it.

**Every exception in this module means "no verdict", and a caller that cannot get a
verdict must refuse the action.** The engine answers a question ("may this happen?"),
so a client that turns a dead socket, a timeout, or a malformed frame into a
permission has been defeated without the engine ever being consulted. That is why
every failure is an exception rather than a sentinel: an exception does not silently
compare equal to anything.

The classes are split by *where* the failure happened, because the caller's options
differ:

* :class:`DogwoodTransportError` — no answer. Retrying a read-only call is fine;
  retrying a **submit** is not (see :class:`DogwoodTimeoutError`).
* :class:`DogwoodEngineError` — the engine answered, and its answer was an error.
  For an install this usually carries the validation findings, which are worth
  showing a human.
"""

from __future__ import annotations


class DogwoodError(Exception):
    """Base class. Catching this catches every way of not getting a verdict."""


class DogwoodTransportError(DogwoodError):
    """The engine could not be reached, or the conversation broke mid-frame.

    Nothing about the request can be concluded from this — least of all that it was
    allowed.
    """


class DogwoodProtocolError(DogwoodTransportError):
    """A frame arrived that the protocol does not allow.

    A subclass of :class:`DogwoodTransportError` on purpose: a client that handles
    the transport case has handled this one, and there is no useful action that
    differs between them.
    """


class DogwoodTimeoutError(DogwoodTransportError):
    """The deadline passed with no answer.

    **Genuinely ambiguous, and therefore not retryable for a submit.** The engine may
    have appended the event and decided, or never seen it; it does not deduplicate,
    so a blind retry can double-append a history event and change what a `formerly`
    or `count_within` rule sees afterwards. Refuse the action, and resolve the
    ambiguity out of band.
    """


class DogwoodEngineError(DogwoodError):
    """The engine answered with its own error variant.

    On the data plane that means the request could not be served. On the control
    plane it is usually a rejected policy bundle — the engine validates before
    accepting and swaps atomically, so the running set is untouched — and
    :attr:`message` carries the findings.
    """

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class DogwoodAuthError(DogwoodError):
    """The remote adapter refused the request for want of a valid credential.

    Only reachable over HTTP: the Unix transport has no credential to present — the
    kernel attests its callers, and an unattested one is refused at `connect()` with a
    plain `EACCES` (a :class:`DogwoodTransportError`).

    Kept separate from :class:`DogwoodEngineError` on purpose: the engine never saw the
    request, so its message says nothing about policy, and an operator debugging a 401
    should not go looking in the policy set. It is still a
    :class:`DogwoodError` — and therefore still a refusal, not a verdict.
    """
