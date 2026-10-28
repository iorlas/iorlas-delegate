"""The process table from ``/proc``, and liveness that survives pid reuse.

A pid alone names no process: once its holder is reaped the number is handed to a stranger.
The pair (pid, start token) does: the token is the process's start time in clock ticks since
boot (``/proc/<pid>/stat`` field 22), which a reused pid cannot share. No ``ps``, no external
tool. Where the OS keeps no ``/proc`` (macOS) there is no table and no token: ``proc_start``
says ``None``, ``alive`` falls back to signal 0 on the pid, and the group signal alone carries
a stop (``stream_proc.tree``).
"""

from __future__ import annotations

import contextlib
import os
from dataclasses import dataclass
from pathlib import Path

PROC = Path("/proc")


@dataclass(frozen=True, slots=True)
class Proc:
    """One row of the process table. ``state`` is the kernel's letter (``Z`` zombie)."""

    pid: int
    ppid: int
    pgid: int
    sid: int
    start: str
    state: str


def has_proc() -> bool:
    return (PROC / "self" / "stat").is_file()


def read(pid: int) -> Proc | None:
    """``/proc/<pid>/stat`` as a ``Proc``, or None when it is gone or unreadable.

    The command name may hold spaces and parentheses, so the fields are split after the last ``)``.
    """
    with contextlib.suppress(OSError, ValueError, IndexError):
        rest = (PROC / str(int(pid)) / "stat").read_text(encoding="utf-8", errors="replace").rpartition(")")[2].split()
        return Proc(int(pid), int(rest[1]), int(rest[2]), int(rest[3]), rest[19], rest[0])
    return None


def table() -> dict[int, Proc]:
    """Every process now, by pid. Empty where there is no ``/proc``."""
    rows: dict[int, Proc] = {}
    with contextlib.suppress(OSError):
        for entry in PROC.iterdir():
            if entry.name.isdigit() and (row := read(int(entry.name))) is not None:
                rows[row.pid] = row
    return rows


def proc_start(pid: int | None) -> str | None:
    """The start token of a live (non-zombie) process; None when it is gone, a zombie, or the OS keeps no ``/proc``."""
    row = read(pid) if pid else None
    return None if row is None or row.state.startswith(("Z", "X")) else row.start


def alive(pid: int | None, start: str | None = None) -> bool:
    """True while ``pid`` is a live process, and, given ``start``, still the one that began at that token.

    Without ``/proc`` the token cannot be checked and a bare signal 0 answers (a reused pid then counts as alive).
    """
    if not pid:
        return False
    if has_proc():
        token = proc_start(pid)
        return token is not None and (not start or token == start)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:  # PermissionError: it exists
        return True
    return True


def descendants(root: int, rows: dict[int, Proc]) -> list[Proc]:
    """Every process under ``root`` in ``rows``, children before grandchildren."""
    kids: dict[int, list[Proc]] = {}
    for row in rows.values():
        kids.setdefault(row.ppid, []).append(row)
    found: list[Proc] = []
    seen = {root}
    todo = [root]
    while todo:
        for kid in kids.get(todo.pop(0), []):
            if kid.pid not in seen:
                seen.add(kid.pid)
                found.append(kid)
                todo.append(kid.pid)
    return found
