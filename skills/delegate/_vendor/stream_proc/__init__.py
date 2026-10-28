"""stream-proc — run a child, stream its lines, stop its whole tree and prove it dead.

- :func:`run` — one child in a session of its own; stdout/stderr lines to callbacks, wall and idle
  timeouts, a stop flag; a typed :class:`Result`.
- :func:`stop_tree` — SIGTERM, grace, SIGKILL, a sweep of descendants that left the group, a proof.
- :func:`alive`, :func:`proc_start` — pid-reuse-safe liveness (``/proc`` on Linux, libproc on macOS);
  :func:`table`, :func:`descendants` — the process table; :func:`group_alive` — is any process in a group.
- :class:`TermHold`, :func:`popen_held` — a SIGTERM landing during a spawn still stops the child.

Stdlib only (``ctypes`` on macOS), POSIX.
"""

from stream_proc.hold import TermHold, popen_held
from stream_proc.procfs import Proc
from stream_proc.proctable import alive, descendants, group_alive, proc_start, table
from stream_proc.runner import Kind, Result, run
from stream_proc.tree import stop_tree

__all__ = [
    "Kind",
    "Proc",
    "Result",
    "TermHold",
    "alive",
    "descendants",
    "group_alive",
    "popen_held",
    "proc_start",
    "run",
    "stop_tree",
    "table",
]
