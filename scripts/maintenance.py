"""Nightly maintenance for the memory compiler.

Designed to run unattended from launchd/cron. Does, in order:
  1. flush.py --retry-failed   — drain recoverable failed flushes
  2. lint.py --fix             — mechanical KB repairs (backlinks, index stubs)
  3. weekly (Sunday): lint.py full run with the LLM contradictions check
  4. health.py                 — final status; macOS notification if not ok

All steps are best-effort: a failing step logs and continues so one bad
component doesn't block the rest of the maintenance pass.

Usage:
    uv run python scripts/maintenance.py            # normal nightly pass
    uv run python scripts/maintenance.py --no-notify  # skip notification (tests)
"""

from __future__ import annotations

import argparse
import logging
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = ROOT / "scripts"
LOG_FILE = SCRIPTS_DIR / "maintenance.log"

logging.basicConfig(
    filename=str(LOG_FILE),
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)

WEEKLY_FULL_LINT_WEEKDAY = 6  # Sunday (Monday=0)
STEP_TIMEOUT = 30 * 60  # generous: contradictions check reads the whole KB


def run_step(name: str, cmd: list[str], timeout: int = STEP_TIMEOUT) -> int:
    """Run one maintenance step; log output; never raise."""
    logging.info("step %s: %s", name, " ".join(cmd))
    try:
        result = subprocess.run(
            cmd,
            cwd=str(ROOT),
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        tail = (result.stdout or "")[-2000:]
        if tail.strip():
            logging.info("step %s output (tail):\n%s", name, tail)
        if result.returncode != 0:
            logging.warning("step %s exited %d\nstderr tail:\n%s",
                            name, result.returncode, (result.stderr or "")[-1000:])
        return result.returncode
    except subprocess.TimeoutExpired:
        logging.error("step %s timed out after %ds", name, timeout)
        return -1
    except OSError:
        logging.exception("step %s failed to launch", name)
        return -1


def notify(title: str, message: str) -> None:
    """Best-effort macOS notification via osascript."""
    if sys.platform != "darwin":
        return
    script = f'display notification "{message}" with title "{title}"'
    try:
        subprocess.run(["osascript", "-e", script], capture_output=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        logging.warning("notification failed", exc_info=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Nightly memory-compiler maintenance")
    parser.add_argument("--no-notify", action="store_true", help="Skip macOS notification")
    parser.add_argument(
        "--full-lint",
        action="store_true",
        help="Force the weekly full lint (with LLM contradictions) regardless of weekday",
    )
    args = parser.parse_args(argv)

    now = datetime.now(timezone.utc).astimezone()
    logging.info("=== maintenance pass started ===")

    uv = ["uv", "run", "--directory", str(ROOT), "python"]

    # A large backlog drains at ~20-30s per context (live run: 55 contexts in
    # ~25 min), so this step gets a bigger budget than the default.
    run_step(
        "retry-failed",
        uv + [str(SCRIPTS_DIR / "flush.py"), "--retry-failed"],
        timeout=60 * 60,
    )
    run_step("lint-fix", uv + [str(SCRIPTS_DIR / "lint.py"), "--fix"])

    # Post-compile covers the normal path; this drains anything left over.
    # At 04:30 a locked keychain makes the LLM call fail harmlessly.
    run_step("index-rewrite", uv + [str(SCRIPTS_DIR / "index_rewrite.py")])

    if args.full_lint or now.weekday() == WEEKLY_FULL_LINT_WEEKDAY:
        run_step("lint-full", uv + [str(SCRIPTS_DIR / "lint.py")])

    health_code = run_step("health", uv + [str(SCRIPTS_DIR / "health.py")])

    logging.info("=== maintenance pass finished (health exit %d) ===", health_code)

    if health_code != 0 and not args.no_notify:
        notify(
            "Memory Compiler",
            "Maintenance found issues — run: uv run python scripts/health.py",
        )

    return 0


if __name__ == "__main__":
    sys.exit(main())
