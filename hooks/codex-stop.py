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

# Recursion guard
if os.environ.get("CLAUDE_INVOKED_BY"):
    sys.exit(0)

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from locking import file_lock

CODEX_SESSIONS_DIR = Path.home() / ".codex" / "sessions"
DEDUP_FILE = SCRIPTS_DIR / ".last-codex-import.json"
DEDUP_LOCK_FILE = SCRIPTS_DIR / ".locks" / "codex-stop.lock"

# Only import transcripts modified within this window (seconds) when falling
# back to filesystem scanning.
MAX_AGE = 120
DEDUP_WINDOW = 3600
MAX_RECENT_IMPORTS = 128


def parse_hook_input(raw_input: str) -> dict:
    """Parse the Stop hook JSON payload."""
    raw_input = raw_input.strip()
    if not raw_input:
        return {}
    try:
        payload = json.loads(raw_input)
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}


def load_recent_imports() -> list[dict]:
    if not DEDUP_FILE.exists():
        return []
    try:
        payload = json.loads(DEDUP_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []

    recent = payload.get("recent", [])
    if not isinstance(recent, list):
        return []
    return [item for item in recent if isinstance(item, dict)]


def save_recent_imports(recent: list[dict]) -> None:
    DEDUP_FILE.parent.mkdir(parents=True, exist_ok=True)
    DEDUP_FILE.write_text(json.dumps({"recent": recent}, indent=2), encoding="utf-8")


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


def claim_import_key(import_key: str) -> bool:
    """Atomically reserve an import key, returning False if it was already seen."""
    now = time.time()
    with file_lock(DEDUP_LOCK_FILE):
        recent = [
            item
            for item in load_recent_imports()
            if now - float(item.get("timestamp", 0)) < DEDUP_WINDOW
        ]
        if any(item.get("key") == import_key for item in recent):
            return False

        recent.append({"key": import_key, "timestamp": now})
        save_recent_imports(recent[-MAX_RECENT_IMPORTS:])
        return True


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
    transcript_path_str = hook_input.get("transcript_path")
    if not isinstance(transcript_path_str, str) or not transcript_path_str:
        return None, {}

    transcript = Path(transcript_path_str).expanduser()
    if not transcript.exists():
        return None, {}

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
        "uv",
        "run",
        "--directory",
        str(ROOT),
        "python",
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

    return cmd


def _transcript_mtime_ns(transcript: Path) -> int:
    try:
        return transcript.stat().st_mtime_ns
    except OSError:
        return 0


def main() -> None:
    hook_input = parse_hook_input(sys.stdin.read())

    if should_skip_stop_event(hook_input):
        return

    transcript, meta = resolve_transcript_from_hook(hook_input)
    if transcript is None:
        transcript, meta = resolve_legacy_transcript()
    if transcript is None:
        return

    import_key = build_import_key(
        session_id=meta.get("session_id") or None,
        turn_id=meta.get("turn_id") or None,
        transcript=transcript,
        transcript_mtime_ns=_transcript_mtime_ns(transcript),
    )

    if not claim_import_key(import_key):
        return

    cmd = build_import_command(transcript, meta)

    # Spawn as background process so hook returns quickly
    kwargs: dict = {}
    if sys.platform == "win32":
        kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
    else:
        kwargs["start_new_session"] = True

    try:
        subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            **kwargs,
        )
    except Exception:
        pass


if __name__ == "__main__":
    main()
