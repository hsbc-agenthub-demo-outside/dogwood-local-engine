#!/usr/bin/env python3
"""Can some uid actually reach a socket? Answer precisely, without being that uid.

This exists because the failure it diagnoses is invisible: the engine's state
directory is mode `0700`, so a socket inside it is unreachable by any other
principal **no matter how the socket itself is chmodded**. The socket's own mode is
`0666` and looks perfectly permissive; the refusal happens one or two directories
higher, during path resolution, and surfaces as a bare `EACCES` on `connect()`.

The check walks the path from the root down and reports the first component that
denies search permission to `uid` — which is what an operator needs, because the fix
is almost always "mount that directory somewhere traversable", not "chmod the
socket".

Usage:  socket-reachability.py <path> <uid>
Exit:   0 reachable, 1 not, 2 usage/error.
"""

from __future__ import annotations

import os
import stat
import sys


def blocking_components(path: str, uid: int, gid: int) -> list[tuple[str, str]]:
    """Every ancestor of `path`, plus the socket itself, that `uid` may not use."""
    blocked: list[tuple[str, str]] = []
    parts = os.path.abspath(path).split(os.sep)

    # Every directory from / down to the socket's parent needs search (x) permission.
    for depth in range(1, len(parts)):
        component = os.sep + os.sep.join(parts[1:depth])
        if not component:
            component = os.sep
        if not os.path.lexists(component):
            blocked.append((component, "does not exist"))
            continue
        mode = os.stat(component).st_mode
        if not stat.S_ISDIR(mode):
            blocked.append((component, "not a directory"))
            continue
        if not _searchable(os.stat(component), uid, gid):
            blocked.append((component, f"{oct(stat.S_IMODE(mode))} denies search"))

    # ...and the socket itself needs write permission for connect(2) to succeed.
    if not os.path.lexists(path):
        blocked.append((path, "does not exist"))
    elif not _writable(os.stat(path), uid, gid):
        blocked.append((path, f"{oct(stat.S_IMODE(os.stat(path).st_mode))} denies write"))

    return blocked


def _bits(info: os.stat_result, uid: int, gid: int, bit: int) -> bool:
    """The classic owner/group/other evaluation, including supplementary groups."""
    if info.st_uid == uid:
        return bool(info.st_mode & (bit << 6))
    groups = os.getgroups()
    if info.st_gid == gid or info.st_gid in groups:
        return bool(info.st_mode & (bit << 3))
    return bool(info.st_mode & bit)


def _searchable(info: os.stat_result, uid: int, gid: int) -> bool:
    return _bits(info, uid, gid, stat.S_IXOTH)


def _writable(info: os.stat_result, uid: int, gid: int) -> bool:
    return _bits(info, uid, gid, stat.S_IWOTH)


def main() -> int:
    if len(sys.argv) != 3:
        print(__doc__.strip().splitlines()[-2], file=sys.stderr)
        return 2
    path, uid = sys.argv[1], int(sys.argv[2])
    gid = uid

    blocked = blocking_components(path, uid, gid)
    if not blocked:
        print(f"{path} is reachable by uid {uid}")
        return 0

    print(f"{path} is NOT reachable by uid {uid}:", file=sys.stderr)
    for component, why in blocked:
        print(f"  {component}: {why}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
