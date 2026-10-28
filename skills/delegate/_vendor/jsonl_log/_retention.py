"""Age-based retention: delete files nobody wrote to for longer than a limit."""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterable
    from pathlib import Path


def expire(paths: Iterable[Path], max_age: float, *, now: float | None = None) -> list[Path]:
    """Delete every file in ``paths`` whose last write is more than ``max_age`` seconds before ``now``.

    Returns the files deleted. A path that does not exist, or that vanishes meanwhile (another
    process pruned it first), is skipped. ``now`` defaults to the current time.
    """
    cutoff = (time.time() if now is None else now) - max_age
    gone: list[Path] = []
    for path in paths:
        try:
            if path.stat().st_mtime < cutoff:
                path.unlink()
                gone.append(path)
        except FileNotFoundError:
            continue
    return gone
