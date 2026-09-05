"""Serialize backend selection with database publication and legacy writes."""

from __future__ import annotations

import inspect
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from functools import wraps
from pathlib import Path
from typing import Any

from locking import file_lock
from memory_store import MemoryStore

_DECISIONS: ContextVar[dict[Path, bool]] = ContextVar("memory_backend_decisions", default={})


@contextmanager
def _decision(root: Path, canonical: bool) -> Iterator[bool]:
    token = _DECISIONS.set({**_DECISIONS.get(), root: canonical})
    try:
        yield canonical
    finally:
        _DECISIONS.reset(token)


@contextmanager
def writer_gate(root: Path) -> Iterator[bool]:
    """Select the backend after publication waits; retain the gate for legacy work.

    Nested maintenance inherits the outer decision. This is essential when a
    compiler already owns compile.lock: acquiring migration.lock again there
    would invert the migrator's lock order. Canonical work releases the file
    lock before yielding, so long model calls cannot delay unrelated writers.
    """
    root = root.resolve()
    decisions = _DECISIONS.get()
    if root in decisions:
        yield decisions[root]
        return
    with file_lock(root / "scripts/.locks/migration.lock"):
        if not MemoryStore.is_initialized(root):
            with _decision(root, False) as canonical:
                yield canonical
            return
    with _decision(root, True) as canonical:
        yield canonical


def guard_legacy_writer(root: Callable[[], Path]) -> Callable:
    """Wrap an entry point; resolve its root at invocation time for isolated tests."""
    def decorate(function: Callable) -> Callable:
        if inspect.iscoroutinefunction(function):
            @wraps(function)
            async def async_guard(*args: Any, **kwargs: Any) -> Any:
                with writer_gate(root()):
                    return await function(*args, **kwargs)
            return async_guard

        @wraps(function)
        def sync_guard(*args: Any, **kwargs: Any) -> Any:
            with writer_gate(root()):
                return function(*args, **kwargs)
        return sync_guard
    return decorate
