"""The process table and liveness, one API on both OSes.

Linux reads ``/proc`` (``stream_proc.procfs``); macOS calls ``libproc`` through ``ctypes``
(``stream_proc.procmac``). The choice is made once, by ``sys.platform``; the dylib is only touched on first use.
Same names, same rows (:class:`~stream_proc.procfs.Proc`), same start-token contract on both.
"""

from __future__ import annotations

import os
import sys
from typing import TYPE_CHECKING

from stream_proc import procfs

if TYPE_CHECKING:
    from stream_proc.procfs import Proc

if sys.platform == "darwin":
    from stream_proc import procmac

    def has_table() -> bool:
        return procmac.has_table()

    def table() -> dict[int, Proc]:
        return procmac.table()

    def proc_start(pid: int | None) -> str | None:
        return procmac.proc_start(pid)

    def alive(pid: int | None, start: str | None = None) -> bool:
        return procmac.alive(pid, start)

    def _kids(root: int) -> list[Proc]:
        return procmac.descendants(root)

else:

    def has_table() -> bool:
        return procfs.has_proc()

    def table() -> dict[int, Proc]:
        return procfs.table()

    def proc_start(pid: int | None) -> str | None:
        return procfs.proc_start(pid)

    def alive(pid: int | None, start: str | None = None) -> bool:
        return procfs.alive(pid, start)

    def _kids(root: int) -> list[Proc]:
        return procfs.descendants(root, procfs.table())


def descendants(root: int, rows: dict[int, Proc] | None = None) -> list[Proc]:
    """Every process under ``root``, children before grandchildren: from ``rows`` when given, else read now."""
    return procfs.descendants(root, rows) if rows is not None else _kids(root)


def group_alive(pgid: int) -> bool:
    """True while any process is in group ``pgid`` (``killpg(pgid, 0)``): no process table needed, Linux and macOS.

    A group we may not signal still exists, so a permission error counts as alive. Group ids 0 and 1 and below
    are never probed (0 would be our own group): False.
    """
    if pgid <= 1:
        return False
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True
