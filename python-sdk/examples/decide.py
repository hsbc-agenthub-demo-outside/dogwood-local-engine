#!/usr/bin/env python3
"""The agent's side: ask the engine whether a refund may happen.

Walks the story the demo policy describes, and *asserts* it:

    a refund with no matching approval        → denied
    the approval's outcome is recorded        → acknowledged, no verdict
    the same refund, now covered             → allowed, naming the rule
    a refund of an unapproved amount         → denied again

The last step is the one that makes it a real test: the rule correlates on the
amount, so "something was approved" must not be enough.

    python examples/decide.py --store-dir /var/lib/dogwood --amount 500

Exits non-zero if any step disagrees with the policy.
"""

from __future__ import annotations

import argparse
import sys

from dogwood_sdk import DogwoodClient, DogwoodError

PRINCIPAL = 'AgentCore::User::"alice"'
RESOURCE = 'AgentCore::Account::"main"'
REFUND = "AgentCore::Action::refund"
APPROVE = "AgentCore::Action::approve"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--socket", help="the data socket (overrides --store-dir)")
    parser.add_argument("--store-dir", help="the engine's state directory")
    parser.add_argument("--amount", type=int, default=500)
    args = parser.parse_args()

    try:
        dogwood = DogwoodClient(args.socket, store_dir=args.store_dir)
        print(f"engine version: {dogwood.ping()}")
    except DogwoodError as exc:
        # Fail closed, and say so rather than pretending.
        print(f"cannot reach the engine: {exc}", file=sys.stderr)
        return 2

    amount = args.amount
    refund = {
        "principal": PRINCIPAL,
        "resource": RESOURCE,
        "logged": {"input": {"amount": amount}},
        # NB: the two bags are separate on purpose. `logged` is the durable history
        # future timepoints match against; `context` is what this request reads.
        "context": {"input": {"amount": amount}},
    }

    failures = 0

    # 1. No approval in the window.
    decision = dogwood.decide(REFUND, "request", **refund)
    print(f"1. refund {amount} with no approval      -> {decision}")
    failures += decision.allowed

    # 2. The approval tool ran, and its outcome is recorded. `record` insists on the
    #    acknowledgement, because a history event that silently did not land leaves
    #    every temporal window empty — which looks like a broken policy.
    recorded = dogwood.record(
        APPROVE,
        "response",
        principal=PRINCIPAL,
        resource=RESOURCE,
        logged={"input": {"amount": amount}, "output": {"approved": True}},
        context={"input": {"amount": amount}},
    )
    print(f"2. approval recorded                     -> {recorded}")

    # 3. The same refund, now covered.
    decision = dogwood.decide(REFUND, "request", **refund)
    print(f"3. the same refund                       -> {decision}")
    failures += not decision.allowed

    # 4. A different amount was never approved.
    decision = dogwood.decide(
        REFUND,
        "request",
        **{**refund, "context": {"input": {"amount": amount + 1}}},
    )
    print(f"4. refund {amount + 1} (never approved)     -> {decision}")
    failures += decision.allowed

    if failures:
        print(f"\n{failures} step(s) disagreed with the installed policy", file=sys.stderr)
        return 1
    print("\nall four steps match the installed policy")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
