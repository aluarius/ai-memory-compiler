"""
Codex Stop hook - auto-imports the latest Codex session into the knowledge base.

Fires when a Codex session ends. Finds the most recently modified transcript
in ~/.codex/sessions/ and runs import_session.py on it.

Configure in ~/.codex/hooks.json under "Stop".
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
CODEX_SESSIONS_DIR = Path.home() / ".codex" / "sessions"
DEDUP_FILE = SCRIPTS_DIR / ".last-codex-import"

# Only import transcripts modified within this window (seconds)
MAX_AGE = 120


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


def was_already_imported(transcript: Path) -> bool:
    """Check if this transcript was already imported."""
    if not DEDUP_FILE.exists():
        return False
    try:
        stored = DEDUP_FILE.read_text(encoding="utf-8").strip()
        return stored == str(transcript)
    except OSError:
        return False


def mark_imported(transcript: Path) -> None:
    DEDUP_FILE.parent.mkdir(parents=True, exist_ok=True)
    DEDUP_FILE.write_text(str(transcript), encoding="utf-8")


def read_session_meta(transcript: Path) -> dict:
    """Extract session_id and cwd from the first session_meta entry."""
    with open(transcript, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            if entry.get("type") == "session_meta":
                payload = entry.get("payload", {})
                return {
                    "session_id": str(payload.get("id", "")),
                    "cwd": str(payload.get("cwd", "")),
                }
    return {}


def main() -> None:
    transcript = find_latest_transcript()
    if transcript is None:
        return

    if was_already_imported(transcript):
        return

    meta = read_session_meta(transcript)

    cmd = [
        "uv",
        "run",
        "--directory",
        str(ROOT),
        "python",
        str(SCRIPTS_DIR / "import_session.py"),
        str(transcript),
        "--agent", "codex",
        "--provider", "openai",
    ]

    if meta.get("session_id"):
        cmd.extend(["--session-id", meta["session_id"]])
    if meta.get("cwd"):
        cmd.extend(["--cwd", meta["cwd"]])

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
        mark_imported(transcript)
    except Exception:
        pass


if __name__ == "__main__":
    main()
