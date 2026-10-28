"""stream-proc — run a child, stream its lines, stop its whole tree and prove it dead.

- :func:`run` — one child in a session of its own; stdout/stderr lines to callbacks, wall and idle
  timeouts, a stop flag; a typed :class:`Result`.
- :func:`stop_tree` — SIGTERM, grace, SIGKILL, a sweep of descendants that left the group, a proof.
- :func:`alive`, :func:`proc_start` — pid-reuse-safe liveness from ``/proc``.
- :class:`TermHold`, :func:`popen_held` — a SIGTERM landing during a spawn still stops the child.

Stdlib only, POSIX.
"""

from stream_proc.hold import TermHold, popen_held
from stream_proc.procfs import alive, proc_start
from stream_proc.runner import Kind, Result, run
from stream_proc.tree import stop_tree

__all__ = ["Kind", "Result", "TermHold", "alive", "popen_held", "proc_start", "run", "stop_tree"]
