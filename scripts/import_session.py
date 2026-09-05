"""Import an external AI session into the shared memory pipeline.

This enables non-Claude sources, such as Codex transcripts, to feed the same
daily log -> compile -> knowledge flow.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
HOOKS_DIR = ROOT_DIR / "hooks"
SCRIPTS_DIR = ROOT_DIR / "scripts"
FAILED_FLUSH_DIR = ROOT_DIR / "reports" / "failed-flushes"

if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))
if str(HOOKS_DIR) not in sys.path:
    sys.path.insert(0, str(HOOKS_DIR))

from sanitize import sanitize  # noqa: E402 - scripts and hooks paths are configured above.
from session_utils import parse_transcript  # noqa: E402
from migration_gate import writer_gate  # noqa: E402

MAX_TURNS = 30
MAX_CONTEXT_CHARS = 15_000


def preserve_failed_context(context_file: Path) -> None:
    """Move a context into the normal retry queue when flush.py cannot launch."""
    try:
        FAILED_FLUSH_DIR.mkdir(parents=True, exist_ok=True)
        destination = FAILED_FLUSH_DIR / context_file.name
        if destination.exists():
            timestamp = datetime.now(UTC).astimezone().strftime("%Y%m%d-%H%M%S")
            destination = FAILED_FLUSH_DIR / (
                f"{context_file.stem}-{timestamp}{context_file.suffix}"
            )
        context_file.replace(destination)
    except OSError:
        # The caller preserves the nonzero exit so the failed import remains visible.
        pass


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Import an external session into the memory compiler")
    parser.add_argument("transcript", type=Path, help="Path to a transcript file (.jsonl, .md, .txt)")
    parser.add_argument("--session-id", default=None, help="Stable session id; defaults to transcript stem")
    parser.add_argument("--agent", default="codex", help="Source agent name, e.g. codex")
    parser.add_argument("--provider", default="openai", help="Source provider name")
    parser.add_argument("--model", default=None, help="Optional model identifier")
    parser.add_argument("--cwd", default=None, help="Working directory where the session happened")
    parser.add_argument("--source", default="import", help="Short source label for runtime metadata")
    parser.add_argument(
        "--after-message-count",
        type=int,
        default=0,
        help="For Codex JSONL, import messages after this checkpoint count",
    )
    parser.add_argument(
        "--until-message-count",
        type=int,
        default=None,
        help="For Codex JSONL, stop at this checkpoint count",
    )
    return parser.parse_args()


def _prepare_legacy_import(args: argparse.Namespace) -> tuple[list[str], Path] | None:
    """Persist the legacy handoff while the caller holds the migration gate."""
    transcript = args.transcript
    parsed = parse_transcript(
        transcript,
        max_turns=MAX_TURNS,
        max_chars=MAX_CONTEXT_CHARS,
        after_message_count=max(args.after_message_count, 0),
        until_message_count=args.until_message_count,
    )
    context = parsed.context.strip()
    if not context:
        print("Transcript did not contain usable text context.", file=sys.stderr)
        return None

    context = sanitize(context)
    timestamp = datetime.now(UTC).astimezone().strftime("%Y%m%d-%H%M%S")
    session_id = args.session_id or parsed.session_id or transcript.stem
    range_suffix = f"-{args.after_message_count}-{args.until_message_count or 'latest'}"
    temp_context = SCRIPTS_DIR / f"import-flush-{session_id}{range_suffix}-{timestamp}.md"
    temp_context.write_text(context, encoding="utf-8")

    provider = args.provider or parsed.provider or "openai"
    model = args.model or parsed.model
    cwd = args.cwd or parsed.cwd
    source = args.source
    if source == "import" and parsed.source:
        source = f"import:{parsed.source}"

    cmd = [
        sys.executable,
        str(SCRIPTS_DIR / "flush.py"),
        str(temp_context),
        session_id,
        "--agent",
        args.agent,
        "--provider",
        provider,
        "--source",
        source,
    ]

    if model:
        cmd.extend(["--model", model])
    if cwd:
        cmd.extend(["--cwd", cwd])
    return cmd, temp_context


def main() -> int:
    args = parse_args()
    transcript = args.transcript
    if not transcript.exists():
        print(f"Transcript not found: {transcript}", file=sys.stderr)
        return 1

    with writer_gate(ROOT_DIR) as canonical:
        if canonical:
            from capture_service import capture_transcript
            from memory_store import MemoryStore

            store = MemoryStore(ROOT_DIR)
            ids = capture_transcript(store, transcript, {
                "session_id": args.session_id, "agent": args.agent, "provider": args.provider,
                "model": args.model, "cwd": args.cwd, "source": args.source,
            }, after_message_count=args.after_message_count or None,
                until_message_count=args.until_message_count)
        else:
            prepared = _prepare_legacy_import(args)
            if prepared is None:
                return 1
            cmd, temp_context = prepared

    # A child flush selects its backend under the same gate. Never wait for
    # that child while holding the parent gate; migration may import its
    # already persisted context before the child starts.
    if canonical:
        from flush_service import process_jobs

        for job_id in ids:
            if process_jobs(store, job_id=job_id):
                return 1
        return 0

    completed = subprocess.run(cmd, cwd=str(ROOT_DIR), check=False)
    if completed.returncode != 0:
        with writer_gate(ROOT_DIR) as canonical_after:
            if not canonical_after:
                preserve_failed_context(temp_context)
    return completed.returncode


if __name__ == "__main__":
    raise SystemExit(main())
