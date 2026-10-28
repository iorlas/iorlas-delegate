"""The one append primitive: whole line, one ``write`` call, ``O_APPEND``, so writers in other processes never interleave."""

from __future__ import annotations

import os

#: Mode bits for a new log file, before the umask (what ``open(path, "ab")`` uses).
FILE_MODE = 0o666


def open_append(path: str | os.PathLike[str]) -> int:
    """A descriptor that only appends, creating the file; not inherited by a child process."""
    return os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, FILE_MODE)


def append_line(fd: int, data: bytes) -> int:
    """Write ``data`` (one whole line) to an ``O_APPEND`` descriptor; returns the offset of its first byte.

    The kernel moves to the end of the file and writes under one lock, so two processes
    appending to one file never mix their bytes. A short write (only a full disk does that)
    is finished before returning, and the offset is read back from the descriptor after.
    """
    view = memoryview(data)
    while view:
        view = view[os.write(fd, view) :]
    return os.lseek(fd, 0, os.SEEK_CUR) - len(data)
