"""A SIGTERM or SIGINT (Ctrl-C) that arrives while a child is being spawned must still stop that child.

Python runs a signal handler between bytecodes of the main thread. A handler that raises (SIGINT's
default one raises KeyboardInterrupt), running inside ``Popen()`` or before the caller's ``try``,
makes the spawn raise: the caller never stops the new child, and it (and its tree) runs on, owned by
nobody. ``TermHold`` swaps each handler for one that only notes the signal while the child is being
spawned; ``replay`` puts the real handlers back and runs the noted ones, at a point where the caller
can already stop the child.
"""

from __future__ import annotations

import signal
import subprocess
import threading
from typing import TYPE_CHECKING, Any, Self

if TYPE_CHECKING:
    from types import FrameType, TracebackType

HELD = (signal.SIGTERM, signal.SIGINT)


class TermHold:
    """``with TermHold() as hold: p = Popen(...)``, then ``hold.replay()`` first thing inside the ``try`` that stops ``p``.

    Holds SIGTERM and SIGINT. A no-op off the main thread (signal handlers live there only) and for a
    signal with no Python handler (the default action then ends the process, as it should).
    """

    def __init__(self) -> None:
        self.old: dict[int, Any] = {}
        self.held: list[int] = []

    def __enter__(self) -> Self:
        self.old, self.held = {}, []
        if threading.current_thread() is threading.main_thread():
            for signum in HELD:
                old = signal.getsignal(signum)
                if callable(old):
                    self.old[signum] = old
                    signal.signal(signum, self._note)
        return self

    def __exit__(self, kind: type[BaseException] | None, value: BaseException | None, tb: TracebackType | None) -> bool:
        if kind is not None:  # a failed spawn has nothing to stop: run a held handler now, never lose the signal
            self.replay()
        return False

    def _note(self, signum: int, _frame: FrameType | None) -> None:
        if signum not in self.held:
            self.held.append(signum)

    def release(self) -> None:
        for signum, old in self.old.items():
            signal.signal(signum, old)

    def replay(self) -> None:
        """Put the real handlers back; run the handler of each held signal, in arrival order."""
        self.release()
        held, self.held = self.held, []
        for signum in held:
            self.old[signum](signum, None)


def popen_held(*args: Any, **kwargs: Any) -> tuple[subprocess.Popen[bytes], TermHold]:
    """``(Popen(*args, **kwargs), hold)``; the caller calls ``hold.replay()`` inside the ``try`` that stops the child."""
    with TermHold() as hold:
        return subprocess.Popen(*args, **kwargs), hold
