"""Import an external AI session into the shared memory pipeline.

This enables non-Claude sources, such as Codex transcripts, to feed the same
daily log -> compile -> knowledge flow.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
HOOKS_DIR = ROOT_DIR / "hooks"
SCRIPTS_DIR = ROOT_DIR / "scripts"

if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))
if str(HOOKS_DIR) not in sys.path:
    sys.path.insert(0, str(HOOKS_DIR))

from sanitize import sanitize
from session_utils import parse_transcript

MAX_TURNS = 30
MAX_CONTEXT_CHARS = 15_000


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Import an external session into the memory compiler")
    parser.add_argument("transcript", type=Path, help="Path to a transcript file (.jsonl, .md, .txt)")
    parser.add_argument("--session-id", default=None, help="Stable session id; defaults to transcript stem")
    parser.add_argument("--agent", default="codex", help="Source agent name, e.g. codex")
    parser.add_argument("--provider", default="openai", help="Source provider name")
    parser.add_argument("--model", default=None, help="Optional model identifier")
    parser.add_argument("--cwd", default=None, help="Working directory where the session happened")
    parser.add_argument("--source", default="import", help="Short source label for runtime metadata")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    transcript = args.transcript
    if not transcript.exists():
        print(f"Transcript not found: {transcript}", file=sys.stderr)
        return 1

    parsed = parse_transcript(
        transcript,
        max_turns=MAX_TURNS,
        max_chars=MAX_CONTEXT_CHARS,
    )
    context = parsed.context.strip()
    if not context:
        print("Transcript did not contain usable text context.", file=sys.stderr)
        return 1

    context = sanitize(context)
    timestamp = datetime.now(timezone.utc).astimezone().strftime("%Y%m%d-%H%M%S")
    session_id = args.session_id or parsed.session_id or transcript.stem
    temp_context = SCRIPTS_DIR / f"import-flush-{session_id}-{timestamp}.md"
    temp_context.write_text(context, encoding="utf-8")

    provider = args.provider or parsed.provider or "openai"
    model = args.model or parsed.model
    cwd = args.cwd or parsed.cwd
    source = args.source
    if source == "import" and parsed.source:
        source = f"import:{parsed.source}"

    cmd = [
        "uv",
        "run",
        "--directory",
        str(ROOT_DIR),
        "python",
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

    completed = subprocess.run(cmd, cwd=str(ROOT_DIR), check=False)
    return completed.returncode


if __name__ == "__main__":
    raise SystemExit(main())
