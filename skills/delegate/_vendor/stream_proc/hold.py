"""A SIGTERM that arrives while a child is being spawned must still stop that child.

Python runs a signal handler between bytecodes of the main thread. A handler that raises, running
inside ``Popen()``, makes the constructor raise: the caller never receives the ``Popen`` object and
the new child (and its tree) runs on, owned by nobody. ``TermHold`` swaps the handler for one that
only notes the signal while the child is being spawned; ``replay`` puts the real handler back and
runs it, at a point where the caller can already stop the child.
"""

from __future__ import annotations

import signal
import subprocess
import threading
from typing import TYPE_CHECKING, Any, Self

if TYPE_CHECKING:
    from types import FrameType, TracebackType


class TermHold:
    """``with TermHold() as hold: p = Popen(...)``, then ``hold.replay()`` first thing inside the ``try`` that stops ``p``.

    A no-op off the main thread (signal handlers live there only) and when SIGTERM has no Python
    handler (the default action then ends the process, as it should).
    """

    def __init__(self) -> None:
        self.old: Any = None
        self.held = False

    def __enter__(self) -> Self:
        self.old, self.held = None, False
        if threading.current_thread() is threading.main_thread():
            old = signal.getsignal(signal.SIGTERM)
            if callable(old):
                self.old = old
                signal.signal(signal.SIGTERM, self._note)
        return self

    def __exit__(self, kind: type[BaseException] | None, value: BaseException | None, tb: TracebackType | None) -> bool:
        if kind is not None:  # a failed spawn has nothing to stop: the real handler returns at once
            self.release()
        return False

    def _note(self, _signum: int, _frame: FrameType | None) -> None:
        self.held = True

    def release(self) -> None:
        if self.old is not None:
            signal.signal(signal.SIGTERM, self.old)

    def replay(self) -> None:
        """Put the real handler back; if a SIGTERM was held, run the handler now."""
        self.release()
        if self.held:
            self.held = False
            self.old(signal.SIGTERM, None)


def popen_held(*args: Any, **kwargs: Any) -> tuple[subprocess.Popen[bytes], TermHold]:
    """``(Popen(*args, **kwargs), hold)``; the caller calls ``hold.replay()`` inside the ``try`` that stops the child."""
    with TermHold() as hold:
        return subprocess.Popen(*args, **kwargs), hold
