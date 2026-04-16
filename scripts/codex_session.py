"""Launch interactive Codex and import the resulting session into the KB."""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = ROOT_DIR / "scripts"
CODEX_HOME = Path.home() / ".codex"
SESSION_INDEX_FILE = CODEX_HOME / "session_index.jsonl"
HISTORY_FILE = CODEX_HOME / "history.jsonl"
SESSIONS_DIR = CODEX_HOME / "sessions"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run interactive Codex, then import the finished session into the memory pipeline"
    )
    parser.add_argument("--cwd", type=Path, default=ROOT_DIR, help="Working directory for the Codex session")
    parser.add_argument("--agent", default="codex", help="Agent label written into daily logs")
    parser.add_argument("--provider", default="openai", help="Provider label written into daily logs")
    parser.add_argument("--skip-import", action="store_true", help="Run Codex only, without transcript import")
    parser.add_argument(
        "codex_args",
        nargs=argparse.REMAINDER,
        help="Arguments forwarded to interactive codex; use -- before them",
    )
    return parser.parse_args()


def build_codex_interactive_command(*, cwd: Path, forwarded_args: list[str]) -> list[str]:
    args = list(forwarded_args)
    if args[:1] == ["--"]:
        args = args[1:]

    cmd = ["codex"]
    if not any(arg in {"-C", "--cd"} for arg in args):
        cmd.extend(["-C", str(cwd)])
    cmd.extend(args)
    return cmd


def _iter_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []

    entries: list[dict] = []
    with open(path, encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(obj, dict):
                entries.append(obj)
    return entries


def load_session_index(path: Path = SESSION_INDEX_FILE) -> dict[str, dict]:
    entries: dict[str, dict] = {}
    for entry in _iter_jsonl(path):
        session_id = entry.get("id")
        if isinstance(session_id, str):
            entries[session_id] = entry
    return entries


def load_history_entries(path: Path = HISTORY_FILE) -> list[dict]:
    return _iter_jsonl(path)


def _parse_iso_timestamp(value: object) -> float:
    if not isinstance(value, str) or not value:
        return 0.0
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


def find_new_session_id(before: dict[str, dict], after: dict[str, dict]) -> str | None:
    new_ids = [session_id for session_id in after if session_id not in before]
    if not new_ids:
        return None
    return max(new_ids, key=lambda session_id: _parse_iso_timestamp(after[session_id].get("updated_at")))


def find_touched_session_id(
    before: dict[str, dict],
    after: dict[str, dict],
    *,
    start_ts: float,
) -> str | None:
    threshold = start_ts - 5
    touched: list[tuple[float, str]] = []

    for session_id, entry in after.items():
        updated_at = _parse_iso_timestamp(entry.get("updated_at"))
        if updated_at < threshold:
            continue

        previous = before.get(session_id, {})
        if entry.get("updated_at") == previous.get("updated_at"):
            continue

        touched.append((updated_at, session_id))

    if not touched:
        return None
    return max(touched)[1]


def read_codex_session_meta(transcript_path: Path) -> dict[str, str]:
    session_id = ""
    cwd = ""

    with open(transcript_path, encoding="utf-8") as handle:
        for line in handle:
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
            break

    return {"session_id": session_id, "cwd": cwd}


def find_transcript_for_session(session_id: str, sessions_dir: Path = SESSIONS_DIR) -> Path | None:
    matches = sorted(
        sessions_dir.rglob(f"*{session_id}.jsonl"),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    return matches[0] if matches else None


def find_session_id_from_history(history: list[dict], *, start_ts: float) -> str | None:
    recent: list[tuple[float, str]] = []
    threshold = start_ts - 5
    for entry in history:
        session_id = entry.get("session_id")
        ts = entry.get("ts")
        if not isinstance(session_id, str) or not session_id:
            continue
        if not isinstance(ts, int | float):
            continue
        if ts >= threshold:
            recent.append((float(ts), session_id))

    if not recent:
        return None
    return max(recent)[1]


def find_recent_transcripts(
    *,
    start_ts: float,
    cwd: Path | None,
    sessions_dir: Path = SESSIONS_DIR,
) -> list[Path]:
    threshold = start_ts - 5
    matches: list[tuple[float, Path]] = []

    for transcript in sessions_dir.rglob("rollout-*.jsonl"):
        try:
            mtime = transcript.stat().st_mtime
        except FileNotFoundError:
            continue
        if mtime < threshold:
            continue

        if cwd is not None:
            meta = read_codex_session_meta(transcript)
            if meta.get("cwd") != str(cwd):
                continue

        matches.append((mtime, transcript))

    return [path for _, path in sorted(matches, reverse=True)]


def resolve_transcript_after_run(
    *,
    before_index: dict[str, dict],
    start_ts: float,
    cwd: Path,
    session_index_file: Path = SESSION_INDEX_FILE,
    history_file: Path = HISTORY_FILE,
    sessions_dir: Path = SESSIONS_DIR,
) -> tuple[str | None, Path | None]:
    after_index = load_session_index(session_index_file)

    session_id = find_new_session_id(before_index, after_index)
    if session_id:
        transcript = find_transcript_for_session(session_id, sessions_dir)
        if transcript is not None:
            return session_id, transcript

    session_id = find_touched_session_id(before_index, after_index, start_ts=start_ts)
    if session_id:
        transcript = find_transcript_for_session(session_id, sessions_dir)
        if transcript is not None:
            meta = read_codex_session_meta(transcript)
            if meta.get("cwd") == str(cwd):
                return session_id, transcript

    session_id = find_session_id_from_history(load_history_entries(history_file), start_ts=start_ts)
    if session_id:
        transcript = find_transcript_for_session(session_id, sessions_dir)
        if transcript is not None:
            meta = read_codex_session_meta(transcript)
            if meta.get("cwd") == str(cwd):
                return session_id, transcript

    transcripts = find_recent_transcripts(start_ts=start_ts, cwd=cwd, sessions_dir=sessions_dir)
    if len(transcripts) != 1:
        return None, None

    transcript = transcripts[0]
    meta = read_codex_session_meta(transcript)
    session_id = meta.get("session_id") or None
    return session_id, transcript


def import_transcript(
    *,
    transcript: Path,
    session_id: str | None,
    agent: str,
    provider: str,
) -> int:
    cmd = [
        "uv",
        "run",
        "--directory",
        str(ROOT_DIR),
        "python",
        str(SCRIPTS_DIR / "import_session.py"),
        str(transcript),
        "--agent",
        agent,
        "--provider",
        provider,
    ]

    if session_id:
        cmd.extend(["--session-id", session_id])

    completed = subprocess.run(cmd, cwd=str(ROOT_DIR), check=False)
    return completed.returncode


def main() -> int:
    args = parse_args()
    if shutil.which("codex") is None:
        print("Codex CLI not found in PATH", file=sys.stderr)
        return 1

    cwd = args.cwd.resolve()
    before_index = load_session_index()
    start_ts = time.time()

    cmd = build_codex_interactive_command(cwd=cwd, forwarded_args=args.codex_args)
    completed = subprocess.run(cmd, cwd=str(cwd), check=False)

    if args.skip_import:
        return completed.returncode

    if completed.returncode != 0:
        return completed.returncode

    session_id, transcript = resolve_transcript_after_run(
        before_index=before_index,
        start_ts=start_ts,
        cwd=cwd,
    )
    if transcript is None:
        print("Codex session finished, but no transcript was found for import.", file=sys.stderr)
        return 1

    print(f"Importing Codex transcript: {transcript}")
    return import_transcript(
        transcript=transcript,
        session_id=session_id,
        agent=args.agent,
        provider=args.provider,
    )


if __name__ == "__main__":
    raise SystemExit(main())
