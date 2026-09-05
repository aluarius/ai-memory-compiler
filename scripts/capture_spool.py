"""Nonblocking hook capture with durable, sanitized spool files during cutover."""

from __future__ import annotations

import argparse
import errno
import json
import os
import re
import subprocess
import sys
import time
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from functools import wraps
from pathlib import Path
from typing import Any

from capture_service import MAX_JOB_CHARS, read_messages, sanitize, spawn_worker
from memory_export import ExportConflict, atomic_write
from memory_store import MemoryStore, content_hash, timestamp
from migration_gate import writer_gate

ROOT_DIR = Path(__file__).resolve().parent.parent
_PROVENANCE_KEYS = (
    "agent", "provider", "session_id", "transcript_path", "cwd", "model",
    "after_message_count", "until_message_count",
)


@contextmanager
def try_file_lock(path: Path) -> Iterator[bool]:
    """Attempt the same exclusive OS lock as file_lock without waiting."""
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("a+", encoding="utf-8")
    acquired = False
    try:
        try:
            import fcntl
        except ImportError:
            import msvcrt

            handle.seek(0)
            if not handle.read(1):
                handle.write("\0")
                handle.flush()
            handle.seek(0)
            try:
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                acquired = True
            except OSError as exc:
                if exc.errno not in (errno.EACCES, errno.EAGAIN, errno.EDEADLK):
                    raise
            try:
                yield acquired
            finally:
                if acquired:
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
            except OSError as exc:
                if exc.errno not in (errno.EACCES, errno.EAGAIN):
                    raise
            try:
                yield acquired
            finally:
                if acquired:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    finally:
        handle.close()


def _metadata(values: dict[str, Any]) -> dict[str, Any]:
    """Persist explicit provenance scalars, never an unfiltered hook payload."""
    allowed = {*_PROVENANCE_KEYS, "captured_at", "source", "turn_id", "event_date"}
    return {
        key: sanitize(value) if isinstance(value, str) else value
        for key, value in values.items()
        if key in allowed and isinstance(value, (str, int, float, bool))
    }


def _identity(context: str, metadata: dict[str, Any]) -> str:
    provenance = {key: metadata[key] for key in _PROVENANCE_KEYS if key in metadata}
    return content_hash(json.dumps({"context": context, "provenance": provenance}, ensure_ascii=False, sort_keys=True))


def _sync_directory(directory: Path) -> None:
    """Make the atomic spool publication durable on platforms supporting dir fsync."""
    if os.name == "nt":
        return
    descriptor = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def persist_spooled_context(root: Path, context: str, metadata: dict[str, Any]) -> Path:
    """Persist the complete sanitized source before spawning or returning from a hook."""
    context = sanitize(context)
    if not context.strip():
        raise ValueError("Capture spool requires nonempty context")
    metadata = _metadata(metadata)
    metadata.setdefault("captured_at", timestamp())
    identity = _identity(context, metadata)
    directory = root / "reports/capture-spool"
    path = directory / f"{identity}.json"
    payload = {"version": 1, "identity": identity, "context": context, "metadata": metadata}
    try:
        atomic_write(path, json.dumps(payload, ensure_ascii=False) + "\n")
    except ExportConflict:
        # Concurrent hooks can capture the same source. Keep the first complete
        # file, including its original capture time, when provenance agrees.
        existing = json.loads(path.read_text(encoding="utf-8"))
        if (
            not isinstance(existing, dict) or existing.get("context") != context
            or not isinstance(existing.get("metadata"), dict)
            or _identity(context, existing["metadata"]) != identity
            or existing.get("identity") != identity
        ):
            raise ValueError("Existing capture spool differs from the captured source") from None
    _sync_directory(directory)
    _sync_directory(directory.parent)
    return path


def _failure_event(root: Path, reason: str, *, agent: str, source: str) -> None:
    """Record an operational category without transcript text or raw hook input."""
    path = root / "reports/capture-spool/failures" / f"{uuid.uuid4()}.json"
    atomic_write(path, json.dumps({
        "kind": "capture_spool_failed", "reason": reason,
        "agent": agent, "source": source, "created": timestamp(),
    }) + "\n")


def spawn_spool_importer(root: Path) -> bool:
    """Detach the blocking importer only after a complete spool file is durable."""
    options: dict[str, Any] = {
        "cwd": str(root), "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL,
        "env": {**os.environ, "MEMORY_COMPILER_INTERNAL": "1"},
    }
    if sys.platform == "win32":
        options["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS
    else:
        options["start_new_session"] = True
    try:
        subprocess.Popen([sys.executable, str(root / "scripts/capture_spool.py"), "--root", str(root)], **options)
    except OSError:
        return False
    return True


def _spool_hook(root: Path, raw: str, *, agent: str, source: str) -> int:
    try:
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            payload = json.loads(re.sub(r'(?<!\\)\\(?!["\\])', r'\\\\', raw))
        if not isinstance(payload, dict):
            raise ValueError("Hook input must be an object")
    except (json.JSONDecodeError, ValueError):
        _failure_event(root, "invalid_hook_payload", agent=agent, source=source)
        return 1
    if agent == "codex" and payload.get("stop_hook_active") is True:
        return 0
    transcript_path = payload.get("transcript_path")
    if not isinstance(transcript_path, str) or not transcript_path:
        _failure_event(root, "missing_transcript_path", agent=agent, source=source)
        return 1
    transcript = Path(transcript_path).expanduser()
    try:
        messages, parsed = read_messages(transcript)
        metadata = {**_metadata(parsed), **_metadata(payload), "agent": agent, "source": source}
        metadata["provider"] = metadata.get("provider") or ("openai" if agent == "codex" else "anthropic")
        metadata["session_id"] = metadata.get("session_id") or transcript.stem
        metadata.update({"transcript_path": str(transcript.resolve()),
                         "after_message_count": 0, "until_message_count": len(messages)})
        persist_spooled_context(root, "".join(messages), metadata)
    except (OSError, UnicodeError, ValueError):
        _failure_event(root, "transcript_capture_failed", agent=agent, source=source)
        return 1
    if not spawn_spool_importer(root):
        _failure_event(root, "importer_spawn_failed", agent=agent, source=source)
        return 1
    return 0


def guard_capture_hook(root: Callable[[], Path], *, agent: str, source: str) -> Callable:
    """Use the immediate hook path if unlocked; spool without waiting otherwise."""
    def decorate(function: Callable) -> Callable:
        @wraps(function)
        def guarded(*args: Any, **kwargs: Any) -> Any:
            current = root().resolve()
            with try_file_lock(current / "scripts/.locks/migration.lock") as acquired:
                if acquired:
                    return function(*args, **kwargs)
            try:
                return _spool_hook(current, sys.stdin.read(), agent=agent, source=source)
            except OSError:
                # Even the failure event may be unwritable; the raw transcript
                # remains intact and no ingestion checkpoint was advanced.
                print("Capture spool could not be persisted.", file=sys.stderr)
                return 1
        return guarded
    return decorate


def import_spooled_contexts(
    store: MemoryStore, source_root: Path | None = None, *, archive: bool = True,
) -> list[str]:
    """Atomically enqueue each spool without advancing transcript checkpoints.

    Successful originals are retained in imported/. A staged or cross-root
    migration may pass archive=False to leave the source tree untouched.
    No migration gate is acquired here: migrators can call this while holding it.
    """
    root = (source_root or store.root).resolve()
    if archive and root != store.root:
        raise ValueError("Cross-root spool imports require archive=False")
    imported: list[str] = []
    for path in sorted((root / "reports/capture-spool").glob("*.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(payload, dict) or payload.get("version") != 1:
                raise ValueError("Unsupported spool schema")
            context, meta = payload.get("context"), payload.get("metadata")
            if not isinstance(context, str) or not context.strip() or not isinstance(meta, dict):
                raise ValueError("Invalid spool context or metadata")
            identity = _identity(context, meta)
            if identity != payload.get("identity") or path.stem != identity:
                raise ValueError("Spool content or provenance hash differs")
            context, meta = sanitize(context), _metadata(meta)
            captures = [{
                "context": context[offset:offset + MAX_JOB_CHARS],
                "metadata": {**meta, "part_offset": offset, "part_total_chars": len(context), "capture_spool": identity},
                "identity": f"spool:{identity}:{offset}",
            } for offset in range(0, len(context), MAX_JOB_CHARS) if context[offset:offset + MAX_JOB_CHARS].strip()]
            ids = store.capture_batch(None, expected=None, checkpoint=None, captures=captures)
        except (OSError, UnicodeError, ValueError) as exc:
            store.event("capture_spool_import_failed", f"{path.name}: {type(exc).__name__}")
            continue
        imported.extend(ids)
        if archive:
            directory = path.parent / "imported"
            directory.mkdir(exist_ok=True)
            destination = directory / path.name
            if destination.exists():
                destination = directory / f"{path.stem}-{time.time_ns()}.json"
            try:
                path.replace(destination)
                _sync_directory(directory)
                _sync_directory(path.parent)
            except FileNotFoundError:
                pass  # A concurrent importer already retained the same source.
            except OSError as exc:
                store.event("capture_spool_archive_failed", f"{path.name}: {type(exc).__name__}")
    return list(dict.fromkeys(imported))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT_DIR)
    parser.add_argument("--no-flush", action="store_true", help="Import durably without spawning a flush worker")
    args = parser.parse_args(argv)
    with writer_gate(args.root) as canonical:
        if not canonical:
            return 0  # The spool remains available for cutover or a future drain.
    store = MemoryStore(args.root)
    ids = import_spooled_contexts(store)
    return 0 if args.no_flush or spawn_worker(store, ids) else 1


if __name__ == "__main__":
    raise SystemExit(main())
