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
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from pathlib import Path

from codex_exec import run_codex_prompt
from config import DAILY_LOG_LOCK_FILE, LLM_LOCK_FILE
from locking import file_lock
from runtime_config import get_claude_model, get_codex_model, get_task_runtime
from session_utils import SessionMetadata, format_session_header
from utils import file_hash

ROOT = Path(__file__).resolve().parent.parent
DAILY_DIR = ROOT / "daily"
REPORTS_DIR = ROOT / "reports"
SCRIPTS_DIR = ROOT / "scripts"
STATE_FILE = SCRIPTS_DIR / "last-flush.json"
STATE_LOCK_FILE = SCRIPTS_DIR / ".locks" / "flush-state.lock"
# LLM_LOCK_FILE (imported from config) serializes LLM calls across ALL
# pipeline processes (flush, compile, lint). Concurrent bundled-CLI instances
# crash each other: bursts of codex imports → simultaneous exit-1 failures at
# 01:46:02/:04/:26; a backlog compile died the same way while evening flushes
# were firing.
RUNTIME_EVENTS_FILE = REPORTS_DIR / "runtime-events.md"
RUNTIME_EVENTS_LOCK_FILE = SCRIPTS_DIR / ".locks" / "runtime-events.lock"
LOG_FILE = SCRIPTS_DIR / "flush.log"
FAILED_FLUSH_DIR = REPORTS_DIR / "failed-flushes"

# Set up file-based logging so we can verify the background process ran.
# The parent process sends stdout/stderr to DEVNULL (to avoid the inherited
# file handle bug on Windows), so this is our main observability channel.
logging.basicConfig(
    filename=str(LOG_FILE),
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)

# Exponential-ish backoff: SDK outages observed in the wild last minutes,
# not seconds, so a couple of quick retries plus one patient one.
RETRY_DELAYS = (3, 30, 180)  # seconds between attempts; total attempts = len + 1
MAX_FAILED_RETRIES = 3  # --retry-failed attempts before a context goes permanent
# Don't re-attempt the same failed session more often than this. Without it,
# a burst of successful flushes (each running the opportunistic drain) could
# burn through all MAX_FAILED_RETRIES within minutes and send a session to
# permanent/ during a single transient outage.
RETRY_COOLDOWN_SECONDS = 6 * 3600
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


def preserve_failed_context(context_file: Path) -> Path | None:
    """Move a failed flush context aside so it can be retried or inspected later.

    Long-lived sessions re-import every turn; if their flushes keep failing,
    each failure used to add another timestamped copy (observed: 5 copies of
    one session within an hour). Keep only the newest context per session —
    it supersedes the older snapshots of the same transcript.
    """
    try:
        FAILED_FLUSH_DIR.mkdir(parents=True, exist_ok=True)

        session_id = extract_session_id(context_file.name)
        if session_id:
            for stale in FAILED_FLUSH_DIR.glob(f"*{session_id}*.md"):
                stale.unlink(missing_ok=True)

        destination = FAILED_FLUSH_DIR / context_file.name
        if destination.exists():
            timestamp = datetime.now(timezone.utc).astimezone().strftime("%Y%m%d-%H%M%S")
            destination = FAILED_FLUSH_DIR / f"{context_file.stem}-{timestamp}{context_file.suffix}"
        return context_file.replace(destination)
    except OSError:
        logging.exception("Failed to preserve flush context: %s", context_file)
        return None


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
    total_attempts = len(RETRY_DELAYS) + 1

    for attempt in range(1, total_attempts + 1):
        response = ""
        result_seen = False
        stderr_lines: list[str] = []

        def capture_stderr(line: str) -> None:
            stderr_lines.append(line)
            logging.debug("CLI stderr: %s", line)

        try:
            async for message in query(
                prompt=prompt,
                options=ClaudeAgentOptions(
                    cwd=str(ROOT),
                    model=get_claude_model(),
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
                    result_seen = getattr(message, "subtype", "success") == "success"
            return response.strip()
        except Exception as e:
            # CLI teardown can fail AFTER a successful result was delivered
            # (same pattern as compile.py). Don't burn a retry on it.
            if result_seen and response.strip():
                logging.warning(
                    "Stream teardown error after successful result (attempt %d): %s",
                    attempt,
                    e,
                )
                return response.strip()
            import traceback

            stderr_output = "\n".join(stderr_lines[-20:]) if stderr_lines else "(no stderr captured)"
            logging.error(
                "Agent SDK error (attempt %d/%d): %s\nCLI stderr:\n%s\n%s",
                attempt,
                total_attempts,
                e,
                stderr_output,
                traceback.format_exc(),
            )
            last_error = e
            if attempt <= len(RETRY_DELAYS):
                delay = RETRY_DELAYS[attempt - 1]
                logging.info("Retrying in %d seconds...", delay)
                await asyncio.sleep(delay)

    return f"FLUSH_ERROR: {type(last_error).__name__}: {last_error}"


async def run_flush(context: str) -> str:
    prompt = build_flush_prompt(context)
    try:
        runtime = get_task_runtime("flush")
        model = get_codex_model() if runtime == "codex" else get_claude_model()
        logging.info("Flush runtime: %s (model: %s)", runtime, model or "default")

        # One LLM call at a time across all flush processes — see LLM_LOCK_FILE.
        # Blocking is fine: a queued flush waits ~30s for the one ahead of it.
        with file_lock(LLM_LOCK_FILE):
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


def _has_stale_past_logs(ingested: dict, today_log: str) -> bool:
    """True if any daily log OLDER than today is uncompiled or changed."""
    for log_path in sorted(DAILY_DIR.glob("*.md")):
        if log_path.name >= today_log:
            continue
        prev = ingested.get(log_path.name)
        if not prev or prev.get("hash") != file_hash(log_path):
            return True
    return False


COMPILE_DEBOUNCE_MINUTES = 30


def _compiled_recently(entry: dict, now: datetime) -> bool:
    """True if this log's last compile is within the debounce window."""
    compiled_at = entry.get("compiled_at")
    if not compiled_at:
        return False
    try:
        last = datetime.fromisoformat(compiled_at)
    except ValueError:
        return False
    return (now - last) < timedelta(minutes=COMPILE_DEBOUNCE_MINUTES)


def maybe_trigger_compilation(now: datetime | None = None) -> None:
    """Trigger compile.py when there is finished material to compile.

    Two paths:
    - After COMPILE_AFTER_HOUR: today's log is considered final → full compile
      (original end-of-day behavior), debounced to once per
      COMPILE_DEBOUNCE_MINUTES — an active evening otherwise recompiles the
      day on every flush (observed: 8 compiles in 70 minutes). The tail left
      by a debounced flush lands with the next flush outside the window or
      with the morning backlog pass.
    - Any time of day: logs from PAST days pending (the 22:00 gate misses
      days when the last session ends early — observed 3-day backlog) →
      compile with --skip-today so the still-growing log isn't churned.
    """
    import subprocess as _sp

    if now is None:
        now = datetime.now(timezone.utc).astimezone()
    today_log = f"{now.strftime('%Y-%m-%d')}.md"

    ingested: dict = {}
    compile_state_file = SCRIPTS_DIR / "state.json"
    if compile_state_file.exists():
        try:
            compile_state = json.loads(compile_state_file.read_text(encoding="utf-8"))
            ingested = compile_state.get("ingested", {})
        except (json.JSONDecodeError, OSError):
            pass

    skip_today = False
    if now.hour >= COMPILE_AFTER_HOUR:
        if today_log in ingested:
            log_path = DAILY_DIR / today_log
            if log_path.exists() and ingested[today_log].get("hash") == file_hash(log_path):
                if not _has_stale_past_logs(ingested, today_log):
                    return
            if _compiled_recently(ingested[today_log], now) and not _has_stale_past_logs(
                ingested, today_log
            ):
                logging.info(
                    "End-of-day compilation debounced (compiled < %d min ago)",
                    COMPILE_DEBOUNCE_MINUTES,
                )
                return
    else:
        if not _has_stale_past_logs(ingested, today_log):
            return
        skip_today = True

    compile_script = SCRIPTS_DIR / "compile.py"
    if not compile_script.exists():
        return

    if skip_today:
        logging.info("Daytime backlog compilation triggered (past-day logs pending)")
    else:
        logging.info("End-of-day compilation triggered (after %d:00)", COMPILE_AFTER_HOUR)

    cmd = ["uv", "run", "--directory", str(ROOT), "python", str(compile_script)]
    if skip_today:
        cmd.append("--skip-today")

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


# ---------------------------------------------------------------------------
# Failed-flush lifecycle (--retry-failed)
#
# preserve_failed_context() stockpiles contexts in reports/failed-flushes/.
# This mode drains them: dedup per session (keep newest copy), retry the
# flush, delete all copies on success. After MAX_FAILED_RETRIES unsuccessful
# retries a session's contexts move to failed-flushes/permanent/ and stop
# being retried — health.py surfaces those for manual review.
# ---------------------------------------------------------------------------

PERMANENT_FAILED_DIR = FAILED_FLUSH_DIR / "permanent"
RETRY_STATE_FILE = FAILED_FLUSH_DIR / "retry-state.json"

_SESSION_ID_RE = None  # compiled lazily; see extract_session_id


def extract_session_id(filename: str) -> str | None:
    """Pull the session UUID out of a failed-context filename.

    Filenames look like session-flush-<uuid>-<ts>.md, flush-context-<uuid>.md,
    import-flush-<uuid>-<ts>-<ts2>.md — the one stable token is the UUID.
    """
    global _SESSION_ID_RE
    if _SESSION_ID_RE is None:
        import re

        _SESSION_ID_RE = re.compile(
            r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
        )
    m = _SESSION_ID_RE.search(filename)
    return m.group(0) if m else None


def load_retry_state() -> dict:
    if RETRY_STATE_FILE.exists():
        try:
            return json.loads(RETRY_STATE_FILE.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass
    return {}


def save_retry_state(state: dict) -> None:
    RETRY_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    RETRY_STATE_FILE.write_text(json.dumps(state, indent=2), encoding="utf-8")


def group_failed_contexts() -> dict[str, list[Path]]:
    """Group failed context files by session id. Unparseable names are skipped."""
    groups: dict[str, list[Path]] = {}
    if not FAILED_FLUSH_DIR.exists():
        return groups
    for f in FAILED_FLUSH_DIR.glob("*.md"):
        session_id = extract_session_id(f.name)
        if session_id is None:
            logging.warning("retry-failed: cannot parse session id from %s, skipping", f.name)
            continue
        groups.setdefault(session_id, []).append(f)
    return groups


def move_to_permanent(files: list[Path]) -> None:
    PERMANENT_FAILED_DIR.mkdir(parents=True, exist_ok=True)
    for f in files:
        try:
            f.replace(PERMANENT_FAILED_DIR / f.name)
        except OSError:
            logging.exception("retry-failed: could not move %s to permanent", f.name)


def _in_retry_cooldown(entry: dict, now: datetime) -> bool:
    """True if this session was attempted within RETRY_COOLDOWN_SECONDS."""
    last_attempt = entry.get("last_attempt")
    if not last_attempt:
        return False
    try:
        last_dt = datetime.fromisoformat(last_attempt)
    except ValueError:
        return False
    return (now - last_dt).total_seconds() < RETRY_COOLDOWN_SECONDS


def retry_failed_flushes(limit: int | None = None, force: bool = False) -> int:
    """Drain reports/failed-flushes/. Returns count of successfully recovered sessions.

    With `limit`, processes at most that many sessions (plain sorted order,
    deterministic). Used by the opportunistic post-flush drain to bound runtime.

    Sessions attempted within RETRY_COOLDOWN_SECONDS are skipped unless
    `force` is set (the manual CLI invocation forces; automated paths don't).
    """
    groups = group_failed_contexts()

    # Prune orphaned retry-state entries: a session whose files are all gone
    # (recovered out-of-band, or moved to permanent via a differently-named
    # copy) leaves a dangling counter that would never be revisited. Drop it
    # so retry-state.json doesn't accumulate dead entries forever.
    retry_state = load_retry_state()
    orphans = [sid for sid in retry_state if sid not in groups]
    if orphans:
        for sid in orphans:
            retry_state.pop(sid, None)
        save_retry_state(retry_state)
        logging.info("retry-failed: pruned %d orphaned retry-state entr(ies)", len(orphans))

    if not groups:
        logging.info("retry-failed: nothing to retry")
        return 0

    recovered = 0
    processed = 0
    now = datetime.now(timezone.utc).astimezone()

    for session_id, files in sorted(groups.items()):
        if limit is not None and processed >= limit:
            break

        state_entry = retry_state.get(session_id, {})
        if not force and _in_retry_cooldown(state_entry, now):
            logging.info("retry-failed: session %s in cooldown, skipping", session_id)
            continue

        processed += 1
        files.sort(key=lambda f: f.stat().st_mtime)
        newest = files[-1]
        duplicates = files[:-1]

        attempts = state_entry.get("attempts", 0)
        if attempts >= MAX_FAILED_RETRIES:
            logging.warning(
                "retry-failed: session %s exceeded %d retries, moving %d file(s) to permanent",
                session_id,
                MAX_FAILED_RETRIES,
                len(files),
            )
            move_to_permanent(files)
            retry_state.pop(session_id, None)
            save_retry_state(retry_state)
            continue

        context = newest.read_text(encoding="utf-8").strip()
        if not context:
            logging.info("retry-failed: %s is empty, dropping all copies", session_id)
            for f in files:
                f.unlink(missing_ok=True)
            retry_state.pop(session_id, None)
            save_retry_state(retry_state)
            continue

        logging.info(
            "retry-failed: session %s, attempt %d/%d, %d chars (%d duplicate copies)",
            session_id,
            attempts + 1,
            MAX_FAILED_RETRIES,
            len(context),
            len(duplicates),
        )

        response = asyncio.run(run_flush(context))

        if response.startswith("FLUSH_ERROR"):
            retry_state.setdefault(session_id, {})["attempts"] = attempts + 1
            retry_state[session_id]["last_error"] = response[:200]
            retry_state[session_id]["last_attempt"] = datetime.now(
                timezone.utc
            ).astimezone().isoformat(timespec="seconds")
            # Persist immediately: if this run is killed mid-pass (maintenance
            # timeout, crash), lost increments would let a never-succeeding
            # session dodge the permanent limit forever.
            save_retry_state(retry_state)
            logging.error("retry-failed: session %s still failing: %s", session_id, response[:120])
            continue

        if response != "FLUSH_OK":
            metadata = SessionMetadata(
                session_id=session_id,
                agent="unknown",
                provider="unknown",
                source="retry-failed",
            )
            append_to_daily_log(response, metadata, "Session (recovered)")
            logging.info("retry-failed: session %s recovered (%d chars)", session_id, len(response))
        else:
            logging.info("retry-failed: session %s had nothing worth saving", session_id)

        for f in files:
            f.unlink(missing_ok=True)
        retry_state.pop(session_id, None)
        save_retry_state(retry_state)
        recovered += 1

    return recovered


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Extract long-term memory from session context")
    parser.add_argument("context_file", type=Path, nargs="?")
    parser.add_argument("session_id", type=str, nargs="?")
    parser.add_argument("--agent", default="claude_code")
    parser.add_argument("--provider", default="anthropic")
    parser.add_argument("--model", default=None)
    parser.add_argument("--cwd", default=None)
    parser.add_argument("--source", default=None)
    parser.add_argument(
        "--retry-failed",
        action="store_true",
        help="Retry preserved failed-flush contexts instead of flushing a new session",
    )
    args = parser.parse_args()
    if not args.retry_failed and (args.context_file is None or args.session_id is None):
        parser.error("context_file and session_id are required unless --retry-failed is given")
    return args


def main() -> None:
    args = parse_args()

    if args.retry_failed:
        # Manual invocation: the user wants retries now — bypass the cooldown.
        recovered = retry_failed_flushes(force=True)
        logging.info("retry-failed: done, %d session(s) recovered", recovered)
        print(f"Recovered {recovered} failed flush session(s)")
        return

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
        flush_failed = False
    elif response.startswith("FLUSH_ERROR"):
        logging.error("Result: %s", response)
        append_runtime_event("flush-error", response, metadata)
        preserved_path = preserve_failed_context(context_file)
        if preserved_path:
            logging.error("Failed flush context preserved: %s", preserved_path)
        flush_failed = True
    else:
        logging.info("Result: saved to daily log (%d chars)", len(response))
        append_to_daily_log(response, metadata, "Session")
        flush_failed = False

    if not flush_failed:
        remember_flush(metadata.session_id, context_hash)
        context_file.unlink(missing_ok=True)
        maybe_trigger_compilation()
        # Opportunistic drain: this environment just proved the SDK works
        # (keychain unlocked, no outage), which the 04:30 launchd pass can't
        # guarantee. Bounded so a backlog never delays the hook pipeline.
        try:
            drained = retry_failed_flushes(limit=2)
            if drained:
                logging.info("Opportunistic drain recovered %d session(s)", drained)
        except Exception:
            logging.exception("Opportunistic drain failed")

    if flush_failed:
        logging.error("Flush failed for session %s", metadata.session_id)
    else:
        logging.info("Flush complete for session %s", metadata.session_id)


if __name__ == "__main__":
    main()
