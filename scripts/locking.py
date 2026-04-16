"""Cross-platform file locking helpers for background workers."""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, TextIO


@contextmanager
def file_lock(path: Path) -> Iterator[TextIO]:
    """Acquire an exclusive lock for the duration of the context."""
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("a+", encoding="utf-8")
    handle.seek(0)

    try:
        try:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        except ImportError:
            import msvcrt

            handle.write("\0")
            handle.flush()
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)

        yield handle
    finally:
        try:
            try:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            except ImportError:
                import msvcrt

                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        finally:
            handle.close()
