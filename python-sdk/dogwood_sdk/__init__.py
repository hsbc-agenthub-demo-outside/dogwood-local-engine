"""A dependency-free Python client for the Dogwood policy engine.

Dogwood answers one question — "given what has happened, may this happen?" — and
this package asks it over the engine's own Unix-socket protocol: a 4-byte
big-endian length, then that many bytes of JSON. No HTTP, no generated bindings, no
third-party packages: the whole wire format is :mod:`dogwood_sdk.framing`.

```python
from dogwood_sdk import DogwoodClient

dogwood = DogwoodClient(store_dir="/tmp/dw")            # the agent's side
decision = dogwood.decide(
    "AgentCore::Action::refund", "request",
    principal='AgentCore::User::"alice"',
    context={"input": {"amount": 500}},
)
if not decision.allowed:
    return "refused"
```

There is nothing to close: a client holds a socket path and a deadline, and opens a
connection per call (see :mod:`dogwood_sdk.client` for why).

# The one rule

**The engine issues verdicts; the caller enforces them.** Nothing here intercepts a
tool call, stops a process, or authenticates the ``principal`` in an event — that is
the harness's job, and a client that treats "could not reach the engine" as "go
ahead" has been defeated without the engine ever being consulted.

Every failure raises (see :mod:`dogwood_sdk.errors`), so a caller that lets an
exception escape does the safe thing. A caller that would rather not write the
``except`` can use :meth:`dogwood_sdk.DogwoodClient.allows`, which returns ``False``
on every failure by construction.

# Two clients, two sockets, two privileges

* :class:`DogwoodClient` — the data plane. Submit events, get verdicts. This is what
  the monitored process may hold, and it *cannot* express a policy edit.
* :class:`DogwoodAdminClient` — the control plane. Author policy. Its socket is
  ``0700`` and the engine authorizes its callers by kernel-attested uid, so being
  able to connect already means you are the operator.
"""

from .client import DogwoodAdminClient, DogwoodClient, control_socket, data_socket
from .errors import (
    DogwoodAuthError,
    DogwoodEngineError,
    DogwoodError,
    DogwoodProtocolError,
    DogwoodTimeoutError,
    DogwoodTransportError,
)
from .framing import MAX_FRAME, read_frame, write_frame
from .transport import HttpTransport, TcpTransport, Transport, UnixTransport, transport_for
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

__version__ = "0.1.0"

__all__ = [
    "MAX_FRAME",
    "Applied",
    "Batched",
    "Decision",
    "DogwoodAdminClient",
    "DogwoodAuthError",
    "DogwoodClient",
    "DogwoodEngineError",
    "DogwoodError",
    "DogwoodProtocolError",
    "DogwoodTimeoutError",
    "DogwoodTransportError",
    "HttpTransport",
    "PolicyPage",
    "PolicySummary",
    "Recorded",
    "Schemas",
    "Status",
    "SubmitResult",
    "TcpTransport",
    "Transport",
    "UnixTransport",
    "__version__",
    "control_socket",
    "data_socket",
    "read_frame",
    "transport_for",
    "write_frame",
]
