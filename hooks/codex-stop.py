"""
Codex Stop hook - auto-imports the current Codex transcript into the KB.

Codex Stop is a turn-scoped lifecycle hook. Newer Codex builds pass the
current `transcript_path`, `session_id`, and `turn_id` on stdin, so this hook
uses the official hook payload first and falls back to transcript scanning only
for older builds.

Configure in ~/.codex/hooks.json or <repo>/.codex/hooks.json under "Stop".
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

# Recursion guard. ``MEMORY_COMPILER_INTERNAL`` is inherited by service-side
# ``codex exec`` calls so their lifecycle hooks never import their own prompts.
if os.environ.get("CLAUDE_INVOKED_BY") or os.environ.get("MEMORY_COMPILER_INTERNAL"):
    sys.exit(0)

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from locking import file_lock  # noqa: E402 - scripts path is configured above.
from capture_spool import guard_capture_hook, parse_hook_payload, require_transcript_path  # noqa: E402
from session_utils import codex_message_ranges  # noqa: E402

CODEX_SESSIONS_DIR = Path.home() / ".codex" / "sessions"
DEDUP_FILE = SCRIPTS_DIR / ".last-codex-import.json"
DEDUP_LOCK_FILE = SCRIPTS_DIR / ".locks" / "codex-stop.lock"

# Only import transcripts modified within this window (seconds) when falling
# back to filesystem scanning.
MAX_AGE = 120
DEDUP_WINDOW = 3600
MAX_RECENT_IMPORTS = 128
CHECKPOINT_TTL_SECONDS = 7 * 24 * 60 * 60
MAX_IMPORT_TURNS = 30
MAX_IMPORT_CONTEXT_CHARS = 15_000


class ImportReservation:
    __slots__ = ("after_message_count", "until_message_count")

    def __init__(self, *, after_message_count: int, until_message_count: int) -> None:
        self.after_message_count = after_message_count
        self.until_message_count = until_message_count


def parse_hook_input(raw_input: str) -> dict:
    """Allow absent legacy stdin, but never treat malformed input as absence."""
    return parse_hook_payload(raw_input, allow_empty=True)


def load_import_state() -> dict:
    if not DEDUP_FILE.exists():
        return {"recent": [], "session_checkpoints": {}}
    try:
        payload = json.loads(DEDUP_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {"recent": [], "session_checkpoints": {}}
    return payload if isinstance(payload, dict) else {"recent": [], "session_checkpoints": {}}


def save_import_state(payload: dict) -> None:
    DEDUP_FILE.parent.mkdir(parents=True, exist_ok=True)
    DEDUP_FILE.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def _is_within_window(value: object, *, now: float, window: int) -> bool:
    """Return whether an untrusted state timestamp is still within a time window."""
    try:
        timestamp = float(value)
    except (TypeError, ValueError):
        return False
    return now - timestamp < window


def find_latest_transcript() -> Path | None:
    """Find the most recently modified Codex transcript."""
    if not CODEX_SESSIONS_DIR.exists():
        return None

    now = time.time()
    best: tuple[float, Path] | None = None

    for transcript in CODEX_SESSIONS_DIR.rglob("rollout-*.jsonl"):
        try:
            mtime = transcript.stat().st_mtime
        except OSError:
            continue
        if now - mtime > MAX_AGE:
            continue
        if best is None or mtime > best[0]:
            best = (mtime, transcript)

    return best[1] if best else None


def build_import_key(
    *,
    session_id: str | None,
    turn_id: str | None,
    transcript: Path,
    transcript_mtime_ns: int,
) -> str:
    if session_id and turn_id:
        return f"session:{session_id}:turn:{turn_id}"
    return f"path:{transcript}:mtime:{transcript_mtime_ns}"


def reserve_import(
    import_key: str,
    *,
    session_id: str | None,
    message_count: int | None,
) -> ImportReservation | None:
    """Reserve a Stop import and return its exact unseen Codex message range."""
    now = time.time()
    with file_lock(DEDUP_LOCK_FILE):
        payload = load_import_state()
        recent = [
            item
            for item in payload.get("recent", [])
            if isinstance(item, dict)
            if _is_within_window(item.get("timestamp"), now=now, window=DEDUP_WINDOW)
        ]
        if any(item.get("key") == import_key for item in recent):
            return None

        after_message_count = 0
        if session_id and message_count is not None:
            checkpoints = payload.get("session_checkpoints", {})
            if not isinstance(checkpoints, dict):
                checkpoints = {}
            checkpoints = {
                key: value
                for key, value in checkpoints.items()
                if isinstance(value, dict)
                and _is_within_window(
                    value.get("timestamp"), now=now, window=CHECKPOINT_TTL_SECONDS
                )
            }
            checkpoint = checkpoints.get(session_id, {})
            try:
                after_message_count = int(checkpoint.get("message_count", 0))
            except (AttributeError, TypeError, ValueError):
                after_message_count = 0
            if message_count <= after_message_count:
                return None
            checkpoints[session_id] = {"message_count": message_count, "timestamp": now}
            payload["session_checkpoints"] = checkpoints

        item = {"key": import_key, "timestamp": now}
        if session_id:
            item["session_id"] = session_id

        recent.append(item)
        payload["recent"] = recent[-MAX_RECENT_IMPORTS:]
        save_import_state(payload)
        return ImportReservation(
            after_message_count=after_message_count,
            until_message_count=message_count or 0,
        )


def release_import(
    import_key: str,
    *,
    session_id: str | None,
    reservation: ImportReservation,
) -> None:
    """Undo a reservation when the hook cannot launch its background importer."""
    with file_lock(DEDUP_LOCK_FILE):
        payload = load_import_state()
        recent = payload.get("recent", [])
        if isinstance(recent, list):
            payload["recent"] = [
                item
                for item in recent
                if not (isinstance(item, dict) and item.get("key") == import_key)
            ]

        if session_id and reservation.until_message_count:
            checkpoints = payload.get("session_checkpoints", {})
            if isinstance(checkpoints, dict):
                checkpoint = checkpoints.get(session_id)
                if isinstance(checkpoint, dict):
                    try:
                        checkpoint_count = int(checkpoint.get("message_count", 0))
                    except (TypeError, ValueError):
                        checkpoint_count = 0
                    if checkpoint_count >= reservation.until_message_count:
                        checkpoint["message_count"] = reservation.after_message_count
                        checkpoint["timestamp"] = time.time()
        save_import_state(payload)


def claim_import_key(import_key: str, *, session_id: str | None = None) -> bool:
    """Atomically reserve an import key, returning False if it was already seen."""
    return reserve_import(import_key, session_id=session_id, message_count=None) is not None


def read_session_meta(transcript: Path) -> dict:
    """Extract session metadata from the first session_meta entry."""
    session_id = ""
    cwd = ""
    provider = ""

    with open(transcript, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            if entry.get("type") != "session_meta":
                continue

            payload = entry.get("payload", {})
            if not isinstance(payload, dict):
                break

            session_id = str(payload.get("id", "") or "")
            cwd = str(payload.get("cwd", "") or "")
            provider = str(payload.get("model_provider", "") or "")
            break

    return {
        "session_id": session_id,
        "cwd": cwd,
        "provider": provider,
    }


def resolve_transcript_from_hook(hook_input: dict) -> tuple[Path | None, dict]:
    """Resolve transcript and metadata from the official Stop hook payload."""
    if "transcript_path" not in hook_input:
        return None, {}

    transcript = require_transcript_path(hook_input["transcript_path"])

    meta = read_session_meta(transcript)
    session_id = hook_input.get("session_id")
    cwd = hook_input.get("cwd")
    model = hook_input.get("model")
    turn_id = hook_input.get("turn_id")

    return transcript, {
        "session_id": str(session_id or meta.get("session_id") or ""),
        "cwd": str(cwd or meta.get("cwd") or ""),
        "model": str(model or ""),
        "provider": str(meta.get("provider") or "openai"),
        "turn_id": str(turn_id or ""),
        "source": "hook:stop",
    }


def resolve_legacy_transcript() -> tuple[Path | None, dict]:
    """Fallback for older Codex builds that don't pass hook stdin metadata."""
    transcript = find_latest_transcript()
    if transcript is None:
        return None, {}

    meta = read_session_meta(transcript)
    return transcript, {
        "session_id": meta.get("session_id", ""),
        "cwd": meta.get("cwd", ""),
        "model": "",
        "provider": meta.get("provider", "openai"),
        "turn_id": "",
        "source": "hook:stop-legacy",
    }


def should_skip_stop_event(hook_input: dict) -> bool:
    """Skip recursive Stop continuations for the same turn."""
    return bool(hook_input.get("stop_hook_active"))


def build_import_command(transcript: Path, metadata: dict) -> list[str]:
    cmd = [
        sys.executable,
        str(SCRIPTS_DIR / "import_session.py"),
        str(transcript),
        "--agent",
        "codex",
        "--provider",
        metadata.get("provider") or "openai",
        "--source",
        metadata.get("source") or "hook:stop",
    ]

    session_id = metadata.get("session_id")
    cwd = metadata.get("cwd")
    model = metadata.get("model")

    if session_id:
        cmd.extend(["--session-id", session_id])
    if cwd:
        cmd.extend(["--cwd", cwd])
    if model:
        cmd.extend(["--model", model])
    if "after_message_count" in metadata:
        cmd.extend(["--after-message-count", str(metadata["after_message_count"])])
    if "until_message_count" in metadata:
        cmd.extend(["--until-message-count", str(metadata["until_message_count"])])

    return cmd


def _transcript_mtime_ns(transcript: Path) -> int:
    try:
        return transcript.stat().st_mtime_ns
    except OSError:
        return 0


@guard_capture_hook(lambda: ROOT, agent="codex", source="hook:stop")
def main() -> None:
    hook_input = parse_hook_input(sys.stdin.read())

    if should_skip_stop_event(hook_input):
        return

    transcript, meta = resolve_transcript_from_hook(hook_input)
    if transcript is None:
        transcript, meta = resolve_legacy_transcript()
    if transcript is None:
        return

    from memory_store import MemoryStore
    if MemoryStore.is_initialized(ROOT):
        from capture_service import capture_transcript, spawn_worker
        store = MemoryStore(ROOT)
        ids = capture_transcript(store, transcript, {**meta, "agent": "codex"})
        spawn_worker(store, ids)
        return

    import_key = build_import_key(
        session_id=meta.get("session_id") or None,
        turn_id=meta.get("turn_id") or None,
        transcript=transcript,
        transcript_mtime_ns=_transcript_mtime_ns(transcript),
    )

    message_count: int | None = None
    ranges: list[tuple[int, int]] = []
    try:
        message_count, _ = codex_message_ranges(
            transcript,
            max_turns=MAX_IMPORT_TURNS,
            max_chars=MAX_IMPORT_CONTEXT_CHARS,
        )
        if not message_count:
            message_count = None
    except (OSError, UnicodeError):
        # Keep the legacy one-shot import path available if a future Codex
        # transcript format cannot be counted yet.
        message_count = None

    reservation = reserve_import(
        import_key,
        session_id=meta.get("session_id") or None,
        message_count=message_count,
    )
    if reservation is None:
        return

    if message_count is not None:
        _, ranges = codex_message_ranges(
            transcript,
            after_message_count=reservation.after_message_count,
            until_message_count=reservation.until_message_count,
            max_turns=MAX_IMPORT_TURNS,
            max_chars=MAX_IMPORT_CONTEXT_CHARS,
        )
        if not ranges:
            release_import(
                import_key,
                session_id=meta.get("session_id") or None,
                reservation=reservation,
            )
            return
    else:
        ranges = [(0, 0)]

    # Spawn as background process so hook returns quickly
    kwargs: dict = {}
    if sys.platform == "win32":
        kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
    else:
        kwargs["start_new_session"] = True

    try:
        for after_message_count, until_message_count in ranges:
            import_meta = meta.copy()
            if message_count is not None:
                import_meta["after_message_count"] = after_message_count
                import_meta["until_message_count"] = until_message_count
            cmd = build_import_command(transcript, import_meta)
            subprocess.Popen(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                **kwargs,
            )
    except OSError:
        release_import(
            import_key,
            session_id=meta.get("session_id") or None,
            reservation=reservation,
        )


if __name__ == "__main__":
    raise SystemExit(main())
