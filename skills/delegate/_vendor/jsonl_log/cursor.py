"""Read a JSON-lines file by byte offset: a cursor that survives restarts, and a tail.

An entry's ``start`` is the byte offset of its line; ``end`` is the offset after its newline,
and the cursor a reader keeps. A last line without its newline is still being written: it is
not returned and the cursor stays at its start, so the next read returns it whole (the torn
line waits). A complete line the decoder refuses with ``ValueError`` is skipped, and the cursor
passes it. One writer appends a line with one ``write`` call (see :func:`jsonl_log.append_line`),
so a reader never sees half a line except at the tail.
"""

from __future__ import annotations

import contextlib
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Generic, TypeVar

from jsonl_log._append import append_line, open_append

if TYPE_CHECKING:
    from collections.abc import Callable

_T = TypeVar("_T")

#: How much of the file one read takes.
CHUNK = 1 << 16


@dataclass(frozen=True)
class Entry(Generic[_T]):
    """One decoded line and the byte span ``[start, end)`` it occupies, newline included."""

    start: int
    end: int
    data: _T


def decode_object(line: bytes) -> dict[str, Any]:
    """A line as a JSON object; ``ValueError`` (UTF-8 and JSON errors are one) for anything else."""
    data = json.loads(line.decode("utf-8"))
    if not isinstance(data, dict):
        msg = "a JSON line that is not an object"
        raise ValueError(msg)  # noqa: TRY004 -- the documented refusal a reader skips
    return data


def size(path: Path) -> int:
    """The file's size in bytes; 0 when it does not exist."""
    try:
        return path.stat().st_size
    except OSError:
        return 0


def read_from(path: Path, decode: Callable[[bytes], _T], offset: int = 0, limit: int | None = None) -> tuple[list[Entry[_T]], int]:
    """The entries from byte ``offset`` and the cursor after them.

    ``limit`` caps the entries returned; the cursor then stops after the last one returned (a
    reader that never passes ``limit`` reads to the end). An ``offset`` past the end of the
    file (it was truncated or replaced) reads from 0. A missing file reads nothing and keeps
    the cursor where it was.
    """
    try:
        with path.open("rb") as handle:
            if offset > os.fstat(handle.fileno()).st_size:
                offset = 0
            _ = handle.seek(offset)
            if limit is None:
                return _parse(handle.read(), offset, None, decode)
            buffer = b""
            while True:
                chunk = handle.read(CHUNK)
                buffer += chunk
                entries, cursor = _parse(buffer, offset, limit, decode)
                if not chunk or len(entries) >= limit:
                    return entries, cursor
    except OSError:
        return [], offset


def tail(path: Path, decode: Callable[[bytes], _T], n: int, before: int | None = None) -> list[Entry[_T]]:
    """The last ``n`` complete entries ending at or before byte ``before`` (default: the end), oldest first."""
    end = size(path) if before is None else min(before, size(path))
    if n <= 0 or end == 0:
        return []
    low = end
    raw = b""
    with path.open("rb") as handle:
        while low > 0 and raw.count(b"\n") <= n:
            low = max(0, low - CHUNK)
            _ = handle.seek(low)
            raw = handle.read(end - low)
    if low > 0:  # the window starts inside a line: drop that partial line
        cut = raw.find(b"\n")
        low, raw = low + cut + 1, raw[cut + 1 :]
    entries, _ = _parse(raw, low, None, decode)
    return entries[-n:]


class Jsonl:
    """One JSON-lines file of objects: append with its offset back, read from a cursor, tail.

    The shape the task-loop ``contracts/jsonl.py`` uses, so that copy can be replaced by this one.
    """

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)

    def append(self, data: dict[str, Any]) -> int:
        """Write one line, flushed; returns the offset of its first byte."""
        line = (json.dumps(data, ensure_ascii=False) + "\n").encode("utf-8")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = open_append(self.path)
        try:
            return append_line(fd, line)
        finally:
            os.close(fd)

    def size(self) -> int:
        return size(self.path)

    def read_from(self, offset: int = 0, limit: int | None = None) -> tuple[list[Entry[dict[str, Any]]], int]:
        """(entries from ``offset``, the cursor after them); see :func:`read_from`."""
        return read_from(self.path, decode_object, offset, limit)

    def tail(self, n: int, before: int | None = None) -> list[Entry[dict[str, Any]]]:
        """The last ``n`` complete entries that end at or before ``before``, oldest first."""
        return tail(self.path, decode_object, n, before)


def _parse(raw: bytes, base: int, limit: int | None, decode: Callable[[bytes], _T]) -> tuple[list[Entry[_T]], int]:
    entries: list[Entry[_T]] = []
    pos = 0
    while limit is None or len(entries) < limit:
        newline = raw.find(b"\n", pos)
        if newline < 0:
            break  # a torn line: it waits
        with contextlib.suppress(ValueError):
            entries.append(Entry(base + pos, base + newline + 1, decode(raw[pos:newline])))
        pos = newline + 1
    return entries, base + pos
