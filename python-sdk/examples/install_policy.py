#!/usr/bin/env python3
"""Install a policy set through the control-plane socket.

The operator's side of the API. It needs to reach the engine's control socket, which
is mode 0700 and authorized by kernel-attested uid — so in practice this runs as the
uid that owns the engine process.

    python examples/install_policy.py --store-dir /var/lib/dogwood \
        --policy ../../python-demo/demo_policy.dw \
        --schema ../../python-demo/demo_schema.cedarschema
"""

from __future__ import annotations

import argparse
import sys

from dogwood_sdk import DogwoodAdminClient, DogwoodError


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--socket", help="the control socket (overrides --store-dir)")
    parser.add_argument("--store-dir", help="the engine's state directory")
    parser.add_argument("--policy", required=True, help="the .dw policy file")
    parser.add_argument("--schema", required=True, help="the .cedarschema action schema")
    parser.add_argument("--event-schema", help="the .dwschema event schema (optional)")
    args = parser.parse_args()

    try:
        admin = DogwoodAdminClient(args.socket, store_dir=args.store_dir)
        applied = admin.install_files(args.policy, args.schema, args.event_schema)
    except DogwoodError as exc:
        print(f"install failed: {exc}", file=sys.stderr)
        return 1

    print(f"installed: {applied}")
    if applied.leaves_prospective:
        print(
            "  note: a temporal rule that is new or edited starts with an empty "
            "window, so it does not fully bite until its window has elapsed"
        )

    status = admin.status()
    print(f"status: {status.rule_count} rule(s), decision kinds {list(status.decision_kinds)}")
    if status.partition_key:
        print(f"  sharded on: {list(status.partition_key)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
