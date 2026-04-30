"""
Memory flush agent - extracts important knowledge from conversation context.

Spawned by session-end.py or pre-compact.py as a background process. Reads
pre-extracted conversation context from a .md file, uses the Claude Agent SDK
to decide what's worth saving, and appends the result to today's daily log.

Usage:
    uv run python flush.py <context_file.md> <session_id>
"""

from __future__ import annotations

# Recursion prevention: set this BEFORE any imports that might trigger Claude
import os

os.environ["CLAUDE_INVOKED_BY"] = "memory_flush"

import argparse
import asyncio
import json
import logging
import sys
import time
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path

from codex_exec import run_codex_prompt
from config import DAILY_LOG_LOCK_FILE
from locking import file_lock
from runtime_config import get_codex_model, get_task_runtime
from session_utils import SessionMetadata, format_session_header
from utils import file_hash

ROOT = Path(__file__).resolve().parent.parent
DAILY_DIR = ROOT / "daily"
REPORTS_DIR = ROOT / "reports"
SCRIPTS_DIR = ROOT / "scripts"
STATE_FILE = SCRIPTS_DIR / "last-flush.json"
STATE_LOCK_FILE = SCRIPTS_DIR / ".locks" / "flush-state.lock"
RUNTIME_EVENTS_FILE = REPORTS_DIR / "runtime-events.md"
RUNTIME_EVENTS_LOCK_FILE = SCRIPTS_DIR / ".locks" / "runtime-events.lock"
LOG_FILE = SCRIPTS_DIR / "flush.log"

# Set up file-based logging so we can verify the background process ran.
# The parent process sends stdout/stderr to DEVNULL (to avoid the inherited
# file handle bug on Windows), so this is our main observability channel.
logging.basicConfig(
    filename=str(LOG_FILE),
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)

MAX_RETRIES = 2
RETRY_DELAY = 3  # seconds
COMPILE_AFTER_HOUR = 22  # 10 PM local time
TEMP_MAX_AGE = 3600  # 1 hour
DEDUP_WINDOW_SECONDS = 120
MAX_RECENT_FLUSHES = 64
ALLOWED_FLUSH_HEADINGS = (
    "**Context:**",
    "**Key Exchanges:**",
    "**Decisions Made:**",
    "**Lessons Learned:**",
    "**Action Items:**",
)


def load_flush_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass
    return {"recent": []}


def save_flush_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, indent=2), encoding="utf-8")


def was_recently_flushed(session_id: str, context_hash: str) -> bool:
    with file_lock(STATE_LOCK_FILE):
        state = load_flush_state()
        now = time.time()
        recent = state.get("recent", [])
        return any(
            item.get("session_id") == session_id
            and item.get("context_hash") == context_hash
            and now - item.get("timestamp", 0) < DEDUP_WINDOW_SECONDS
            for item in recent
        )


def remember_flush(session_id: str, context_hash: str) -> None:
    with file_lock(STATE_LOCK_FILE):
        state = load_flush_state()
        now = time.time()

        recent = [
            item
            for item in state.get("recent", [])
            if now - item.get("timestamp", 0) < DEDUP_WINDOW_SECONDS
        ]
        recent.append(
            {
                "session_id": session_id,
                "context_hash": context_hash,
                "timestamp": now,
            }
        )
        state["recent"] = recent[-MAX_RECENT_FLUSHES:]
        save_flush_state(state)


def append_runtime_event(kind: str, message: str, metadata: SessionMetadata) -> None:
    """Persist operational events outside the raw conversation corpus."""
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")
    header = format_session_header(metadata)
    entry = f"## [{timestamp}] {kind}\n{header}\n\n{message.strip()}\n\n"

    with file_lock(RUNTIME_EVENTS_LOCK_FILE):
        if not RUNTIME_EVENTS_FILE.exists():
            RUNTIME_EVENTS_FILE.write_text("# Runtime Events\n\n", encoding="utf-8")
        with open(RUNTIME_EVENTS_FILE, "a", encoding="utf-8") as f:
            f.write(entry)


def append_to_daily_log(content: str, metadata: SessionMetadata, section: str = "Session") -> None:
    """Append meaningful extracted content to today's daily log."""
    today = datetime.now(timezone.utc).astimezone()
    log_path = DAILY_DIR / f"{today.strftime('%Y-%m-%d')}.md"

    DAILY_DIR.mkdir(parents=True, exist_ok=True)

    with file_lock(DAILY_LOG_LOCK_FILE):
        if not log_path.exists():
            log_path.write_text(
                f"# Daily Log: {today.strftime('%Y-%m-%d')}\n\n## Sessions\n\n",
                encoding="utf-8",
            )

        time_str = today.strftime("%H:%M")
        header = format_session_header(metadata)
        entry = f"### {section} ({time_str})\n\n{header}\n\n{content.strip()}\n\n"

        with open(log_path, "a", encoding="utf-8") as f:
            f.write(entry)


def clean_flush_response(content: str) -> str:
    """Drop transcript scaffolding and keep only the structured daily-log sections."""
    stripped = content.strip()
    if stripped == "FLUSH_OK" or stripped.startswith("FLUSH_ERROR"):
        return stripped

    cleaned: list[str] = []
    in_section = False

    for line in stripped.splitlines():
        current = line.rstrip()
        if any(current.startswith(prefix) for prefix in ALLOWED_FLUSH_HEADINGS):
            in_section = True
            cleaned.append(current)
            continue
        if not current:
            if cleaned and cleaned[-1] != "":
                cleaned.append("")
            continue
        if in_section and (current.startswith("- ") or current.startswith("  - ") or current.startswith("\t- ")):
            cleaned.append(current)

    while cleaned and not cleaned[0]:
        cleaned.pop(0)
    while cleaned and not cleaned[-1]:
        cleaned.pop()

    return "\n".join(cleaned).strip() or "FLUSH_OK"


def build_flush_prompt(context: str) -> str:
    return f"""Review the conversation context below and respond with a concise summary
of important items that should be preserved in the daily log.
Do NOT use any tools — just return plain text.

Format your response as a structured daily log entry with these sections:

**Context:** [One line about what the user was working on]

**Key Exchanges:**
- [Important Q&A or discussions]

**Decisions Made:**
- [Any decisions with rationale]

**Lessons Learned:**
- [Gotchas, patterns, or insights discovered]

**Action Items:**
- [Follow-ups or TODOs mentioned]

Skip anything that is:
- Routine tool calls or file reads
- Content that's trivial or obvious
- Trivial back-and-forth or clarification exchanges
- Transcript scaffolding, assistant narration, or meta lines like "Attempting to read..." / "I'll check..."

Only include sections that have actual content. If nothing is worth saving,
respond with exactly: FLUSH_OK

## Conversation Context

{context}"""


async def run_flush_claude(prompt: str) -> str:
    """Use Claude Agent SDK to extract important knowledge from conversation context."""
    from claude_agent_sdk import (
        AssistantMessage,
        ClaudeAgentOptions,
        ResultMessage,
        TextBlock,
        query,
    )

    last_error = None

    for attempt in range(1, MAX_RETRIES + 1):
        response = ""
        stderr_lines: list[str] = []

        def capture_stderr(line: str) -> None:
            stderr_lines.append(line)
            logging.debug("CLI stderr: %s", line)

        try:
            async for message in query(
                prompt=prompt,
                options=ClaudeAgentOptions(
                    cwd=str(ROOT),
                    allowed_tools=[],
                    max_turns=2,
                    stderr=capture_stderr,
                ),
            ):
                if isinstance(message, AssistantMessage):
                    for block in message.content:
                        if isinstance(block, TextBlock):
                            response += block.text
                elif isinstance(message, ResultMessage):
                    pass
            return response.strip()
        except Exception as e:
            import traceback

            stderr_output = "\n".join(stderr_lines[-20:]) if stderr_lines else "(no stderr captured)"
            logging.error(
                "Agent SDK error (attempt %d/%d): %s\nCLI stderr:\n%s\n%s",
                attempt,
                MAX_RETRIES,
                e,
                stderr_output,
                traceback.format_exc(),
            )
            last_error = e
            if attempt < MAX_RETRIES:
                logging.info("Retrying in %d seconds...", RETRY_DELAY)
                await asyncio.sleep(RETRY_DELAY)

    return f"FLUSH_ERROR: {type(last_error).__name__}: {last_error}"


async def run_flush(context: str) -> str:
    prompt = build_flush_prompt(context)
    try:
        runtime = get_task_runtime("flush")
        logging.info("Flush runtime: %s", runtime)

        if runtime == "codex":
            response = await asyncio.to_thread(
                run_codex_prompt,
                prompt,
                cwd=ROOT,
                allow_edits=False,
                model=get_codex_model(),
            )
            return clean_flush_response(response)

        return clean_flush_response(await run_flush_claude(prompt))
    except Exception as e:
        logging.exception("Flush runtime failed")
        return f"FLUSH_ERROR: {type(e).__name__}: {e}"


def maybe_trigger_compilation() -> None:
    """If it's past the compile hour and today's log hasn't been compiled, run compile.py."""
    import subprocess as _sp

    now = datetime.now(timezone.utc).astimezone()
    if now.hour < COMPILE_AFTER_HOUR:
        return

    today_log = f"{now.strftime('%Y-%m-%d')}.md"
    compile_state_file = SCRIPTS_DIR / "state.json"
    if compile_state_file.exists():
        try:
            compile_state = json.loads(compile_state_file.read_text(encoding="utf-8"))
            ingested = compile_state.get("ingested", {})
            if today_log in ingested:
                log_path = DAILY_DIR / today_log
                if log_path.exists() and ingested[today_log].get("hash") == file_hash(log_path):
                    return
        except (json.JSONDecodeError, OSError):
            pass

    compile_script = SCRIPTS_DIR / "compile.py"
    if not compile_script.exists():
        return

    logging.info("End-of-day compilation triggered (after %d:00)", COMPILE_AFTER_HOUR)

    cmd = ["uv", "run", "--directory", str(ROOT), "python", str(compile_script)]

    kwargs: dict = {}
    if sys.platform == "win32":
        kwargs["creationflags"] = _sp.CREATE_NEW_PROCESS_GROUP | _sp.DETACHED_PROCESS
    else:
        kwargs["start_new_session"] = True

    try:
        log_handle = open(str(SCRIPTS_DIR / "compile.log"), "a", encoding="utf-8")
        _sp.Popen(cmd, stdout=log_handle, stderr=_sp.STDOUT, cwd=str(ROOT), **kwargs)
        log_handle.close()  # parent releases its copy; child keeps writing
    except Exception as e:
        logging.error("Failed to spawn compile.py: %s", e)
        try:
            log_handle.close()
        except Exception:
            pass


def cleanup_old_temp_files() -> None:
    """Remove orphaned temp context files older than TEMP_MAX_AGE seconds."""
    now = time.time()
    patterns = ["session-flush-*.md", "flush-context-*.md", "import-flush-*.md"]
    for pattern in patterns:
        for f in SCRIPTS_DIR.glob(pattern):
            try:
                if now - f.stat().st_mtime > TEMP_MAX_AGE:
                    f.unlink()
                    logging.info("Cleaned up stale temp file: %s", f.name)
            except OSError:
                pass


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Extract long-term memory from session context")
    parser.add_argument("context_file", type=Path)
    parser.add_argument("session_id", type=str)
    parser.add_argument("--agent", default="claude_code")
    parser.add_argument("--provider", default="anthropic")
    parser.add_argument("--model", default=None)
    parser.add_argument("--cwd", default=None)
    parser.add_argument("--source", default=None)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    context_file = args.context_file
    metadata = SessionMetadata(
        session_id=args.session_id,
        agent=args.agent,
        provider=args.provider,
        model=args.model,
        cwd=args.cwd,
        transcript_path=str(context_file),
        source=args.source,
    )

    logging.info("flush.py started for session %s, context: %s", metadata.session_id, context_file)

    cleanup_old_temp_files()

    if not context_file.exists():
        logging.error("Context file not found: %s", context_file)
        return

    context = context_file.read_text(encoding="utf-8").strip()
    if not context:
        logging.info("Context file is empty, skipping")
        context_file.unlink(missing_ok=True)
        return

    context_hash = sha256(context.encode("utf-8")).hexdigest()[:16]
    if was_recently_flushed(metadata.session_id, context_hash):
        logging.info("Skipping duplicate flush for session %s", metadata.session_id)
        context_file.unlink(missing_ok=True)
        return

    logging.info("Flushing session %s: %d chars", metadata.session_id, len(context))

    try:
        response = asyncio.run(run_flush(context))
    except Exception as e:
        logging.exception("Unhandled flush failure")
        response = f"FLUSH_ERROR: {type(e).__name__}: {e}"

    if response == "FLUSH_OK":
        logging.info("Result: FLUSH_OK")
    elif response.startswith("FLUSH_ERROR"):
        logging.error("Result: %s", response)
        append_runtime_event("flush-error", response, metadata)
    else:
        logging.info("Result: saved to daily log (%d chars)", len(response))
        append_to_daily_log(response, metadata, "Session")

    remember_flush(metadata.session_id, context_hash)
    context_file.unlink(missing_ok=True)
    maybe_trigger_compilation()

    logging.info("Flush complete for session %s", metadata.session_id)


if __name__ == "__main__":
    main()
