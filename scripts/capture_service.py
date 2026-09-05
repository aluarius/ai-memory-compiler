"""Durable, sanitized transcript capture; no model calls inside lifecycle hooks."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

from memory_store import MemoryStore, StoreConflict, content_hash, timestamp
from session_utils import _codex_message_from_entry, _normalize_claude_content

HOOKS_DIR = Path(__file__).resolve().parent.parent / "hooks"
if str(HOOKS_DIR) not in sys.path:
    sys.path.insert(0, str(HOOKS_DIR))
from sanitize import sanitize  # noqa: E402 - shared hook module is loaded after path setup.

MAX_JOB_CHARS = 16_000
PROVENANCE_KEYS = (
    "agent", "provider", "session_id", "transcript_path", "cwd", "model",
    "after_message_count", "until_message_count",
)


def sanitize_metadata(values: dict[str, Any]) -> dict[str, Any]:
    """Persist only redacted provenance scalars, never arbitrary hook payloads."""
    allowed = {*PROVENANCE_KEYS, "captured_at", "source", "turn_id", "event_date"}
    return {
        key: sanitize(value) if isinstance(value, str) else value
        for key, value in values.items()
        if key in allowed and isinstance(value, (str, int, float, bool))
    }


def _raw_capture_metadata(path: Path, overrides: dict, parsed: dict) -> dict:
    meta = {**{k: v for k, v in parsed.items() if v}, **{k: v for k, v in overrides.items() if v}}
    meta.setdefault("session_id", path.stem)
    meta.setdefault("agent", "claude_code")
    meta.setdefault("captured_at", timestamp())
    meta["transcript_path"] = str(path.resolve())
    return meta


def _legacy_capture_key(meta: dict) -> str:
    return f"{meta['agent']}:{meta['session_id']}:{meta['transcript_path']}"


def capture_metadata(path: Path, overrides: dict, parsed: dict) -> dict:
    """Share sanitized import scope without merging identities that redact alike."""
    raw = _raw_capture_metadata(path, overrides, parsed)
    return {**sanitize_metadata(raw), "capture_identity": content_hash(_legacy_capture_key(raw))}


def read_messages(path: Path) -> tuple[list[str], dict]:
    """Read complete text messages once, retaining oversized messages in full."""
    messages: list[str] = []
    metadata: dict = {}
    raw = path.read_bytes().decode("utf-8")
    if path.suffix not in {".jsonl", ".json"}:
        return [raw] if raw.strip() else [], metadata
    for line in raw.splitlines():
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue  # A writer can still be appending the last JSONL record.
        if not isinstance(entry, dict):
            continue
        payload = entry.get("payload", {})
        if entry.get("type") == "session_meta" and isinstance(payload, dict):
            metadata.update({"session_id": payload.get("id"), "cwd": payload.get("cwd"),
                             "provider": payload.get("model_provider"), "agent": "codex"})
        if entry.get("type") == "turn_context" and isinstance(payload, dict):
            metadata["model"] = payload.get("model")
        message = _codex_message_from_entry(entry)
        if message is None:
            msg = entry.get("message", entry)
            if isinstance(msg, dict) and msg.get("role") in {"user", "assistant"}:
                text = _normalize_claude_content(msg.get("content", "")).strip()
                if text:
                    message = (msg["role"], text)
        if message:
            role, text = message
            messages.append(f"**{role.title()}:** {text}\n\n")
    return messages, metadata


def legacy_coverage(store: MemoryStore, messages: list[str], metadata: dict) -> tuple[set[int], int]:
    """Reuse only legacy ranges backed by retained full context, not reservations."""
    session = metadata["session_id"]
    reservation = store.get_state("codex_import", {}).get("session_checkpoints", {}).get(session, {})
    reserved = reservation.get("message_count", 0)
    reserved = reserved if isinstance(reserved, int) and reserved > 0 else 0
    covered: set[int] = set()
    if not reserved:
        return covered, 0
    for job in store.jobs():
        meta = job["metadata"]
        if meta.get("session_id") != session or meta.get("agent") != metadata["agent"]:
            continue
        start, end = meta.get("after"), meta.get("until")
        if not isinstance(start, int) or not isinstance(end, int) or not 0 <= start < end <= len(messages):
            continue
        if sanitize("".join(messages[start:end])).strip() == job["context"].strip():
            covered.update(range(start, end))
    # Old reservations preceded durable capture and cannot prove successful
    # extraction. Uncovered history is explicitly recaptured, never discarded.
    return covered, reserved


def capture_transcript(store: MemoryStore, path: Path, metadata: dict, *,
                       after_message_count: int | None = None,
                       until_message_count: int | None = None) -> list[str]:
    """Atomically capture unseen text and advance its checkpoint, never before it.

    Explicit ranges are idempotent imports, not global checkpoint advances: a
    caller may import an isolated range without having captured its predecessors.
    Transcript rewrites fail closed; historical checkpoint prefixes cannot drift.
    """
    messages, parsed = read_messages(path)
    raw_meta = _raw_capture_metadata(path, metadata, parsed)
    meta = capture_metadata(path, metadata, parsed)
    legacy_key = _legacy_capture_key(raw_meta)
    key = meta["capture_identity"]
    explicit = after_message_count is not None or until_message_count is not None
    for _ in range(4):
        checkpoints = store.get_state("capture_checkpoints", {}) if not explicit else {}
        previous = checkpoints.get(key, checkpoints.get(legacy_key))
        start = max(after_message_count or 0, 0) if explicit else (previous or {}).get("message_count", 0)
        end = min(until_message_count, len(messages)) if until_message_count is not None else len(messages)
        if previous and (start > len(messages) or content_hash("".join(messages[:start])) != previous["prefix_hash"]):
            raise StoreConflict("Captured transcript prefix changed; retained jobs were not overwritten")
        if start >= end:
            if not explicit and legacy_key in checkpoints:
                try:
                    store.capture_batch(key, expected=previous, checkpoint=previous,
                                        captures=[], legacy_key=legacy_key)
                except StoreConflict:
                    continue
            return []
        captures = []
        covered, reserved = legacy_coverage(store, messages, raw_meta) if previous is None and not explicit else (set(), 0)
        ranges = []
        position = start
        while position < end:
            if position in covered:
                position += 1
                continue
            beginning = position
            while position < end and position not in covered:
                position += 1
            ranges.append((beginning, position))
        for beginning, ending in ranges:
            context = sanitize("".join(messages[beginning:ending]))
            # Split after sanitization so boundary-spanning secrets are redacted.
            for offset in range(0, len(context), MAX_JOB_CHARS):
                part = context[offset:offset + MAX_JOB_CHARS]
                if part.strip():
                    part_meta = {**meta, "after_message_count": beginning, "until_message_count": ending,
                                 "part_offset": offset, "part_total_chars": len(context),
                                 "legacy_unverified": beginning < reserved}
                    captures.append({"context": part, "metadata": part_meta,
                                     # _enqueue hashes this identity; retain old dedup keys.
                                     "identity": f"{legacy_key}:{beginning}:{ending}:{offset}"})
        checkpoint = {"message_count": end, "prefix_hash": content_hash("".join(messages[:end])),
                      "captured_at": meta["captured_at"]}
        try:
            return store.capture_batch(None if explicit else key, expected=previous,
                                       checkpoint=checkpoint, captures=captures,
                                       legacy_key=None if explicit else legacy_key)
        except StoreConflict:
            continue
    raise StoreConflict("Concurrent capture did not settle; transcript remains available for retry")


def spawn_worker(store: MemoryStore, job_ids: list[str]) -> bool:
    """Launch a bounded drain after durable capture; launch failure loses no data."""
    if not job_ids:
        return True
    command = [sys.executable, str(store.root / "scripts" / "flush.py"), "--drain",
               "--limit", str(len(job_ids) + 2)]
    kwargs: dict = {"cwd": str(store.root), "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}
    if sys.platform == "win32":
        kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
    else:
        kwargs["start_new_session"] = True
    try:
        subprocess.Popen(command, **kwargs)
    except OSError as exc:
        store.event("capture_spawn_failed", f"{type(exc).__name__}: {exc}")
        return False
    return True
