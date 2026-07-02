"""Git versioning for the knowledge/ directory.

knowledge/ is gitignored by the outer repo, so a nested git repository is
invisible to it. Every compile checkpoints before writing and commits after,
which makes interrupted or failed compiles recoverable with a hard rollback
instead of a manual reconciliation pass. An inflight marker distinguishes
partial compile writes from legitimate outside changes (lint fixes, manual
edits) after a kill -9.
"""

from __future__ import annotations

import json
import shutil
import subprocess

from config import KNOWLEDGE_DIR, LOCKS_DIR, now_iso

INFLIGHT_FILE = LOCKS_DIR / "compile-inflight.json"

# Explicit identity: commits must not depend on the user's global git config.
_GIT_IDENTITY = [
    "-c", "user.name=memory-compiler",
    "-c", "user.email=memory-compiler@localhost",
]


def git_available() -> bool:
    return shutil.which("git") is not None


def _git(args: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(KNOWLEDGE_DIR), *_GIT_IDENTITY, *args],
        capture_output=True,
        text=True,
    )


def _repo_exists() -> bool:
    return git_available() and (KNOWLEDGE_DIR / ".git").exists()


def ensure_kb_repo() -> bool:
    """Initialize a git repo inside knowledge/ if missing. Returns True if created."""
    if not git_available() or not KNOWLEDGE_DIR.exists():
        return False
    if (KNOWLEDGE_DIR / ".git").exists():
        return False
    _git(["init", "-q"])
    gitignore = KNOWLEDGE_DIR / ".gitignore"
    if not gitignore.exists():
        gitignore.write_text(".obsidian/\n.DS_Store\n", encoding="utf-8")
    kb_commit("init: knowledge base snapshot")
    return True


def kb_is_dirty() -> bool:
    if not _repo_exists():
        return False
    return bool(_git(["status", "--porcelain"]).stdout.strip())


def kb_commit(message: str) -> bool:
    """Commit all pending knowledge/ changes. Returns True if a commit was made."""
    if not kb_is_dirty():
        return False
    _git(["add", "-A"])
    result = _git(["commit", "-q", "-m", message])
    if result.returncode != 0:
        print(f"  Warning: kb git commit failed: {result.stderr.strip()}")
        return False
    return True


def kb_rollback() -> bool:
    """Discard ALL uncommitted knowledge/ changes, tracked and untracked."""
    if not _repo_exists():
        return False
    reset = _git(["reset", "-q", "--hard", "HEAD"])
    clean = _git(["clean", "-fdq"])
    return reset.returncode == 0 and clean.returncode == 0


def mark_inflight(log_name: str) -> None:
    INFLIGHT_FILE.parent.mkdir(parents=True, exist_ok=True)
    INFLIGHT_FILE.write_text(
        json.dumps({"log": log_name, "started": now_iso()}), encoding="utf-8"
    )


def read_inflight() -> str | None:
    if not INFLIGHT_FILE.exists():
        return None
    try:
        return json.loads(INFLIGHT_FILE.read_text(encoding="utf-8")).get("log")
    except (json.JSONDecodeError, OSError):
        return None


def clear_inflight() -> None:
    INFLIGHT_FILE.unlink(missing_ok=True)


def recover_interrupted_compile() -> bool:
    """Roll back partial writes left by a compile that died mid-run.

    Called at the start of every compile run (under compile.lock). Only acts
    when the inflight marker exists — a dirty repo without a marker is
    legitimate outside work (lint fixes, manual edits) and is left alone.
    Returns True if a rollback happened.
    """
    log_name = read_inflight()
    if log_name is None:
        return False
    rolled_back = False
    if kb_is_dirty():
        rolled_back = kb_rollback()
        if rolled_back:
            print(
                "  Recovered: rolled back partial writes from interrupted "
                f"compile of {log_name}"
            )
    clear_inflight()
    return rolled_back
