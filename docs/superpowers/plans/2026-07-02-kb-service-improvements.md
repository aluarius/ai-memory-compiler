# KB Service Improvements Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the memory-compiler pipeline recoverable (git-versioned KB), self-cleaning (index summary rewrite + monthly consolidation), and usage-aware (better search scoring + read telemetry feeding session-start hub selection).

**Architecture:** A nested git repo inside `knowledge/` (invisible to the outer repo — it's gitignored) gives every compile a checkpoint/commit/rollback cycle plus an inflight marker for kill-9 recovery. Two new LLM passes reuse the existing runtime/lock plumbing: a batched single-turn summary rewrite (deterministic application, no agent edits) and an agent-driven consolidation pass whose deletions go through a manifest applied by script. MCP `read_article` writes a usage counter that session-start blends into hub selection.

**Tech Stack:** Python 3.11+, claude-agent-sdk / codex CLI (via existing `runtime_config` + `LLM_LOCK_FILE` serialization), pytest, git CLI via subprocess.

## Global Constraints

- Run tests with `uv run python -m pytest` (bare `pytest` / stale `.venv` are broken on this machine).
- Every new module starts with `from __future__ import annotations`; follow existing docstring/comment style.
- All LLM calls MUST serialize through `file_lock(LLM_LOCK_FILE)` — concurrent bundled-CLI instances crash each other.
- Claude-runtime calls MUST use `get_claude_model()` (pins claude-opus-4-8); never inherit the interactive default.
- Hooks (`hooks/*.py`) stay stdlib-only — no imports from `scripts/`.
- LLM passes triggered from compile are best-effort: they must never fail the compile.
- Conventional commit messages, NO AI attribution / Co-Authored-By.
- `knowledge/` and `daily/` stay in the outer repo's `.gitignore`.

---

### Task 1: `kb_git` module — git versioning for knowledge/

**Files:**
- Create: `scripts/kb_git.py`
- Test: `tests/test_kb_git.py`

**Interfaces:**
- Produces: `git_available() -> bool`, `ensure_kb_repo() -> bool`, `kb_is_dirty() -> bool`, `kb_commit(message: str) -> bool`, `kb_rollback() -> bool`, `mark_inflight(log_name: str) -> None`, `read_inflight() -> str | None`, `clear_inflight() -> None`, `recover_interrupted_compile() -> bool`. Module constants `KNOWLEDGE_DIR` (from config) and `INFLIGHT_FILE = LOCKS_DIR / "compile-inflight.json"` are read at call time so tests can monkeypatch them.

- [ ] **Step 1: Write failing tests** (`tests/test_kb_git.py`)

```python
from __future__ import annotations

from pathlib import Path

import kb_git


def _setup_kb(monkeypatch, tmp_path: Path) -> Path:
    knowledge_dir = tmp_path / "knowledge"
    knowledge_dir.mkdir()
    (knowledge_dir / "index.md").write_text("# Index\n", encoding="utf-8")
    monkeypatch.setattr(kb_git, "KNOWLEDGE_DIR", knowledge_dir)
    monkeypatch.setattr(kb_git, "INFLIGHT_FILE", tmp_path / "compile-inflight.json")
    return knowledge_dir


def test_ensure_kb_repo_initializes_and_commits(monkeypatch, tmp_path: Path) -> None:
    kb = _setup_kb(monkeypatch, tmp_path)
    assert kb_git.ensure_kb_repo() is True
    assert (kb / ".git").exists()
    assert kb_git.kb_is_dirty() is False
    assert kb_git.ensure_kb_repo() is False  # idempotent


def test_kb_commit_and_dirty_cycle(monkeypatch, tmp_path: Path) -> None:
    kb = _setup_kb(monkeypatch, tmp_path)
    kb_git.ensure_kb_repo()
    (kb / "concepts").mkdir()
    (kb / "concepts" / "new.md").write_text("body", encoding="utf-8")
    assert kb_git.kb_is_dirty() is True
    assert kb_git.kb_commit("compile test") is True
    assert kb_git.kb_is_dirty() is False
    assert kb_git.kb_commit("nothing to do") is False


def test_kb_rollback_discards_tracked_and_untracked(monkeypatch, tmp_path: Path) -> None:
    kb = _setup_kb(monkeypatch, tmp_path)
    kb_git.ensure_kb_repo()
    (kb / "index.md").write_text("# Index\nmutated\n", encoding="utf-8")
    (kb / "partial.md").write_text("partial write", encoding="utf-8")
    assert kb_git.kb_rollback() is True
    assert (kb / "index.md").read_text(encoding="utf-8") == "# Index\n"
    assert not (kb / "partial.md").exists()


def test_inflight_marker_roundtrip(monkeypatch, tmp_path: Path) -> None:
    _setup_kb(monkeypatch, tmp_path)
    assert kb_git.read_inflight() is None
    kb_git.mark_inflight("2026-07-01.md")
    assert kb_git.read_inflight() == "2026-07-01.md"
    kb_git.clear_inflight()
    assert kb_git.read_inflight() is None


def test_recover_interrupted_compile_requires_marker(monkeypatch, tmp_path: Path) -> None:
    kb = _setup_kb(monkeypatch, tmp_path)
    kb_git.ensure_kb_repo()
    (kb / "partial.md").write_text("partial", encoding="utf-8")

    assert kb_git.recover_interrupted_compile() is False  # no marker: keep changes
    assert (kb / "partial.md").exists()

    kb_git.mark_inflight("2026-07-01.md")
    assert kb_git.recover_interrupted_compile() is True
    assert not (kb / "partial.md").exists()
    assert kb_git.read_inflight() is None


def test_kb_functions_noop_without_repo(monkeypatch, tmp_path: Path) -> None:
    _setup_kb(monkeypatch, tmp_path)  # no ensure_kb_repo -> no .git
    assert kb_git.kb_is_dirty() is False
    assert kb_git.kb_commit("x") is False
    assert kb_git.kb_rollback() is False
```

- [ ] **Step 2: Run to verify failure** — `uv run python -m pytest tests/test_kb_git.py -v` → FAIL (`ModuleNotFoundError: kb_git`)

- [ ] **Step 3: Implement `scripts/kb_git.py`**

```python
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
```

- [ ] **Step 4: Run to verify pass** — `uv run python -m pytest tests/test_kb_git.py -v` → all PASS
- [ ] **Step 5: Commit** — `git add scripts/kb_git.py tests/test_kb_git.py && git commit -m "feat: git versioning module for knowledge/"`

---

### Task 2: Wire kb_git into compile.py

**Files:**
- Modify: `scripts/compile.py` (main loop, ~line 274-348)
- Test: `tests/test_compile.py` (append)

**Interfaces:**
- Consumes: everything `kb_git` produces (Task 1).
- Produces: compile main loop calls, in order per log: `kb_commit("checkpoint before compile of <log>")` → `mark_inflight(<log>)` → compile → on failure `kb_rollback()` + `clear_inflight()`; on success `kb_commit("compile <log>")` + `clear_inflight()`. After archive: `kb_commit("post-compile maintenance (lint fixes, index rewrite, archive)")`.

- [ ] **Step 1: Write failing tests** (append to `tests/test_compile.py`; add `import sys`, `import pytest` at top if missing)

```python
def _wire_main(monkeypatch, tmp_path, compile_result):
    """Patch everything main() touches; return the kb-call recorder."""
    calls: list[str] = []

    daily = tmp_path / "daily"
    daily.mkdir()
    log = daily / "2026-07-01.md"
    log.write_text("session notes", encoding="utf-8")

    monkeypatch.setattr(compile_script, "list_raw_files", lambda: [log])
    monkeypatch.setattr(compile_script, "load_state", lambda: {"ingested": {}})
    monkeypatch.setattr(compile_script, "LOCKS_DIR", tmp_path / ".locks")
    monkeypatch.setattr(compile_script, "list_wiki_articles", lambda: [])
    monkeypatch.setattr(compile_script, "run_post_compile_lint", lambda: 0)
    monkeypatch.setattr(compile_script, "archive_old_logs", lambda: None)
    monkeypatch.setattr(compile_script, "maybe_run_consolidation", lambda: None)
    monkeypatch.setattr(compile_script, "run_summary_rewrite_best_effort", lambda: None)

    async def fake_compile(path):
        calls.append(f"compile:{path.name}")
        return compile_result

    monkeypatch.setattr(compile_script, "compile_daily_log", fake_compile)

    def record(name, result=True):
        def fn(*args):
            calls.append(name if not args else f"{name}:{args[0]}")
            return result
        return fn

    for name in ("ensure_kb_repo", "recover_interrupted_compile", "kb_rollback"):
        monkeypatch.setattr(compile_script, name, record(name))
    monkeypatch.setattr(compile_script, "kb_commit", record("kb_commit"))
    monkeypatch.setattr(compile_script, "mark_inflight", record("mark_inflight", None))
    monkeypatch.setattr(compile_script, "clear_inflight", record("clear_inflight", None))
    monkeypatch.setattr(sys, "argv", ["compile.py"])
    return calls


def test_main_checkpoints_and_commits_on_success(monkeypatch, tmp_path) -> None:
    calls = _wire_main(monkeypatch, tmp_path, compile_result=0.5)

    compile_script.main()

    assert "ensure_kb_repo" in calls
    assert "recover_interrupted_compile" in calls
    idx = calls.index("compile:2026-07-01.md")
    assert "kb_commit:checkpoint before compile of 2026-07-01.md" in calls[:idx]
    assert "mark_inflight:2026-07-01.md" in calls[:idx]
    assert "kb_commit:compile 2026-07-01.md" in calls[idx:]
    assert "clear_inflight" in calls[idx:]
    assert not any(c == "kb_rollback" for c in calls)
    assert calls[-1].startswith("kb_commit:post-compile maintenance")


def test_main_rolls_back_on_failed_compile(monkeypatch, tmp_path) -> None:
    calls = _wire_main(monkeypatch, tmp_path, compile_result=None)

    with pytest.raises(SystemExit):
        compile_script.main()

    idx = calls.index("compile:2026-07-01.md")
    assert "kb_rollback" in calls[idx:]
    assert "clear_inflight" in calls[idx:]
    assert not any(c == "kb_commit:compile 2026-07-01.md" for c in calls)
```

- [ ] **Step 2: Run to verify failure** — `uv run python -m pytest tests/test_compile.py -v` → FAIL (missing attrs)

- [ ] **Step 3: Implement.** In `scripts/compile.py`:
  - add import: `from kb_git import (clear_inflight, ensure_kb_repo, kb_commit, kb_rollback, mark_inflight, recover_interrupted_compile)`
  - add a placeholder used by later tasks so tests can patch it now (replaced for real in Tasks 5/7):

```python
def run_summary_rewrite_best_effort() -> None:  # implemented in Task 5
    pass


def maybe_run_consolidation() -> None:  # implemented in Task 7
    pass
```

  - in `main()`, right after the `if args.dry_run: return` gate:

```python
        ensure_kb_repo()
        recover_interrupted_compile()
```

  - wrap the compile loop body:

```python
        for i, log_path in enumerate(to_compile, 1):
            print(f"\n[{i}/{len(to_compile)}] Compiling {log_path.name}...")
            kb_commit(f"checkpoint before compile of {log_path.name}")
            mark_inflight(log_path.name)
            cost = asyncio.run(compile_daily_log(log_path))
            if cost is None:
                if kb_rollback():
                    print("  Rolled back partial knowledge/ writes.")
                clear_inflight()
                failed_logs.append(log_path.name)
                print("  Failed.")
                break
            kb_commit(f"compile {log_path.name}")
            clear_inflight()
            total_cost += cost
            print("  Done.")
```

  - at the end of `main()`:

```python
        run_summary_rewrite_best_effort()
        archive_old_logs()
        kb_commit("post-compile maintenance (lint fixes, index rewrite, archive)")
        maybe_run_consolidation()
```

- [ ] **Step 4: Run full suite** — `uv run python -m pytest` → PASS
- [ ] **Step 5: Commit** — `git commit -m "feat(compile): checkpoint/commit/rollback via knowledge git repo"`

---

### Task 3 (folded into Task 4, step 1-2): shared index-row regex

Move the row regex to utils so lint, index_rewrite, and consolidate share one definition (session-start keeps its own copy — hooks are stdlib-only by design).

---

### Task 4: `index_rewrite` module — batched LLM summary rewrite

**Files:**
- Modify: `scripts/utils.py` (add `INDEX_ROW_RE`), `scripts/lint.py` (use it)
- Create: `scripts/index_rewrite.py`
- Test: `tests/test_index_rewrite.py`

**Interfaces:**
- Produces in utils: `INDEX_ROW_RE` — compiled regex, groups: (target, summary, sources, updated).
- Produces in index_rewrite: `collect_rewrite_targets() -> list[dict]` (`{target, summary, kind: 'long'|'stub', excerpt}`), `build_rewrite_prompt(targets) -> str`, `parse_rewrite_response(response, targets) -> dict[str, str]`, `apply_rewrites(rewrites) -> int`, `async run_summary_rewrite(batch_size=50) -> int`, CLI `main()` with `--dry-run`. Constants `MAX_SUMMARY_CHARS = 200`, `STUB_MARKER = "auto-stub: refine summary on next compile"`.

- [ ] **Step 1: Move regex to utils.** In `scripts/utils.py` (after the slug helpers):

```python
# One index table row: | [[target]] | summary | sources | YYYY-MM-DD |
INDEX_ROW_RE = re.compile(
    r"^\|\s*\[\[([^\]]+)\]\]\s*\|\s*(.*?)\s*\|\s*(.*?)\s*\|\s*([0-9]{4}-[0-9]{2}-[0-9]{2})\s*\|\s*$"
)
```

In `scripts/lint.py`: delete the `_INDEX_ROW_RE = re.compile(...)` block, add `INDEX_ROW_RE` to the `from utils import (...)` list, replace the one usage `_INDEX_ROW_RE.match` → `INDEX_ROW_RE.match`.

- [ ] **Step 2: Run suite to confirm no regression** — `uv run python -m pytest tests/test_lint.py -v` → PASS

- [ ] **Step 3: Write failing tests** (`tests/test_index_rewrite.py`)

```python
from __future__ import annotations

import asyncio
from pathlib import Path

import index_rewrite


HEADER = "# Index\n\n| Article | Summary | Compiled From | Updated |\n|---|---|---|---|\n"


def _setup_index(monkeypatch, tmp_path: Path, rows: list[str]) -> Path:
    knowledge_dir = tmp_path / "knowledge"
    (knowledge_dir / "concepts").mkdir(parents=True)
    index = knowledge_dir / "index.md"
    index.write_text(HEADER + "\n".join(rows) + "\n", encoding="utf-8")
    monkeypatch.setattr(index_rewrite, "INDEX_FILE", index)
    monkeypatch.setattr(index_rewrite, "KNOWLEDGE_DIR", knowledge_dir)
    return index


def test_collect_targets_finds_long_and_stub_rows(monkeypatch, tmp_path: Path) -> None:
    long_summary = "x" * 250
    rows = [
        f"| [[concepts/bloated]] | {long_summary} | daily/a.md | 2026-06-01 |",
        "| [[concepts/stubbed]] | Stubbed (auto-stub: refine summary on next compile) | daily/a.md | 2026-06-01 |",
        "| [[concepts/clean]] | fine | daily/a.md | 2026-06-01 |",
    ]
    index = _setup_index(monkeypatch, tmp_path, rows)
    article = index.parent / "concepts" / "stubbed.md"
    article.write_text("---\ntitle: Stubbed\n---\n\nReal body content here.\n", encoding="utf-8")

    targets = index_rewrite.collect_rewrite_targets()

    by_target = {t["target"]: t for t in targets}
    assert by_target["concepts/bloated"]["kind"] == "long"
    assert by_target["concepts/stubbed"]["kind"] == "stub"
    assert "Real body content" in by_target["concepts/stubbed"]["excerpt"]
    assert "concepts/clean" not in by_target


def test_parse_response_validates_lines(monkeypatch, tmp_path: Path) -> None:
    targets = [
        {"target": "concepts/a", "summary": "x" * 250, "kind": "long", "excerpt": None},
        {"target": "concepts/b", "summary": "y" * 250, "kind": "long", "excerpt": None},
        {"target": "concepts/c", "summary": "z" * 250, "kind": "long", "excerpt": None},
    ]
    response = "\n".join([
        "concepts/a: Short essence line.",
        "[[concepts/b]]: Bracketed target accepted.",
        "concepts/c: bad " + "c" * 300,          # too long -> rejected
        "concepts/unknown: not requested",        # unknown -> rejected
        "concepts/a: pipe | breaks tables",       # would overwrite a; rejected
    ])

    parsed = index_rewrite.parse_rewrite_response(response, targets)

    assert parsed == {
        "concepts/a": "Short essence line.",
        "concepts/b": "Bracketed target accepted.",
    }


def test_apply_rewrites_replaces_only_summary_cell(monkeypatch, tmp_path: Path) -> None:
    rows = [
        "| [[concepts/a]] | " + "x" * 250 + " | daily/a.md, daily/b.md | 2026-06-01 |",
        "| [[concepts/keep]] | untouched | daily/k.md | 2026-05-05 |",
    ]
    index = _setup_index(monkeypatch, tmp_path, rows)

    changed = index_rewrite.apply_rewrites({"concepts/a": "New essence."})

    assert changed == 1
    text = index.read_text(encoding="utf-8")
    assert "| [[concepts/a]] | New essence. | daily/a.md, daily/b.md | 2026-06-01 |" in text
    assert "| [[concepts/keep]] | untouched | daily/k.md | 2026-05-05 |" in text


def test_run_summary_rewrite_end_to_end_with_fake_llm(monkeypatch, tmp_path: Path) -> None:
    rows = ["| [[concepts/a]] | " + "x" * 250 + " | daily/a.md | 2026-06-01 |"]
    index = _setup_index(monkeypatch, tmp_path, rows)

    async def fake_llm(prompt: str) -> str:
        assert "concepts/a" in prompt
        return "concepts/a: Rewritten."

    monkeypatch.setattr(index_rewrite, "_call_llm", fake_llm)

    changed = asyncio.run(index_rewrite.run_summary_rewrite())

    assert changed == 1
    assert "Rewritten." in index.read_text(encoding="utf-8")


def test_run_summary_rewrite_survives_llm_failure(monkeypatch, tmp_path: Path) -> None:
    rows = ["| [[concepts/a]] | " + "x" * 250 + " | daily/a.md | 2026-06-01 |"]
    _setup_index(monkeypatch, tmp_path, rows)

    async def broken_llm(prompt: str) -> str:
        raise RuntimeError("SDK down")

    monkeypatch.setattr(index_rewrite, "_call_llm", broken_llm)

    assert asyncio.run(index_rewrite.run_summary_rewrite()) == 0
```

- [ ] **Step 4: Run to verify failure** — `uv run python -m pytest tests/test_index_rewrite.py -v` → FAIL

- [ ] **Step 5: Implement `scripts/index_rewrite.py`**

```python
"""Rewrite bloated knowledge/index.md summary cells with batched LLM calls.

The compiler tends to APPEND history to index summaries instead of rewriting
them (128 rows drifted past the 200-char cap before this existed), and
auto-stub rows linger unrefined. Both classes are detected mechanically; the
LLM returns plain `target: summary` lines and the script applies them
deterministically — no file-editing agent involved.

Usage:
    uv run python scripts/index_rewrite.py            # rewrite all targets
    uv run python scripts/index_rewrite.py --dry-run  # list targets only
"""

from __future__ import annotations

import argparse
import asyncio
import re
from pathlib import Path

from codex_exec import run_codex_prompt
from config import INDEX_FILE, KNOWLEDGE_DIR, LLM_LOCK_FILE
from locking import file_lock
from runtime_config import get_claude_model, get_codex_model, get_task_runtime
from utils import INDEX_ROW_RE

ROOT_DIR = Path(__file__).resolve().parent.parent
MAX_SUMMARY_CHARS = 200
STUB_MARKER = "auto-stub: refine summary on next compile"
BATCH_SIZE = 50
ARTICLE_EXCERPT_CHARS = 1500

# `target: summary`, tolerating [[target]] and stray backticks.
_RESPONSE_LINE_RE = re.compile(r"^\[*([\w/-]+?)\]*\s*:\s*(.+)$")


def _article_excerpt(target: str) -> str:
    path = KNOWLEDGE_DIR / f"{target}.md"
    if not path.exists():
        return "(article file missing)"
    text = path.read_text(encoding="utf-8")
    if text.startswith("---"):
        end = text.find("\n---", 3)
        if end != -1:
            text = text[end + 4:]
    return text.strip()[:ARTICLE_EXCERPT_CHARS]


def collect_rewrite_targets() -> list[dict]:
    """Rows needing a rewrite: over-long summaries and auto-stub placeholders."""
    if not INDEX_FILE.exists():
        return []
    targets = []
    for line in INDEX_FILE.read_text(encoding="utf-8").splitlines():
        m = INDEX_ROW_RE.match(line.strip())
        if not m:
            continue
        target, summary, _sources, _updated = m.groups()
        if STUB_MARKER in summary:
            targets.append({
                "target": target, "summary": summary, "kind": "stub",
                "excerpt": _article_excerpt(target),
            })
        elif len(summary) > MAX_SUMMARY_CHARS:
            targets.append({
                "target": target, "summary": summary, "kind": "long", "excerpt": None,
            })
    return targets


def build_rewrite_prompt(targets: list[dict]) -> str:
    blocks = []
    for t in targets:
        if t["kind"] == "stub":
            blocks.append(
                f"### {t['target']} (stub row — write a real summary from this article excerpt)\n"
                f"{t['excerpt']}"
            )
        else:
            blocks.append(
                f"### {t['target']} (current summary is {len(t['summary'])} chars — compress)\n"
                f"{t['summary']}"
            )
    joined = "\n\n".join(blocks)
    return f"""You maintain the index of a personal knowledge base. Rewrite each
index summary below as ONE line of essence: present state only, no appended
history, no dates, at most {MAX_SUMMARY_CHARS} characters.

Rules:
- Output EXACTLY one line per item, format: <target>: <new summary>
- <target> is the id after '###' (e.g. concepts/foo), copied verbatim
- Plain text only: no '|' characters, no [[wikilinks]], no newlines inside a summary
- Keep concrete technical anchors (tool names, error names, key decisions) over generic phrasing
- Output nothing else — no preamble, no code fences

## Items to rewrite

{joined}"""


def parse_rewrite_response(response: str, targets: list[dict]) -> dict[str, str]:
    """Validate LLM output; silently drop anything malformed or unrequested."""
    requested = {t["target"] for t in targets}
    out: dict[str, str] = {}
    for raw in response.splitlines():
        m = _RESPONSE_LINE_RE.match(raw.strip().strip("`"))
        if not m:
            continue
        target, summary = m.group(1), m.group(2).strip()
        if target not in requested:
            continue
        if not summary or len(summary) > MAX_SUMMARY_CHARS or "|" in summary or "[[" in summary:
            continue
        out[target] = summary
    return out


def apply_rewrites(rewrites: dict[str, str]) -> int:
    """Replace summary cells in index.md; other cells untouched. Returns rows changed."""
    if not rewrites or not INDEX_FILE.exists():
        return 0
    lines = INDEX_FILE.read_text(encoding="utf-8").splitlines()
    changed = 0
    for i, line in enumerate(lines):
        m = INDEX_ROW_RE.match(line.strip())
        if not m:
            continue
        target, old_summary, sources, updated = m.groups()
        new_summary = rewrites.get(target)
        if not new_summary or new_summary == old_summary:
            continue
        lines[i] = f"| [[{target}]] | {new_summary} | {sources} | {updated} |"
        changed += 1
    if changed:
        INDEX_FILE.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return changed


async def _call_llm(prompt: str) -> str:
    runtime = get_task_runtime("lint")
    # Serialize against flush/compile — concurrent bundled-CLI instances crash.
    with file_lock(LLM_LOCK_FILE):
        if runtime == "codex":
            return await asyncio.to_thread(
                run_codex_prompt, prompt,
                cwd=ROOT_DIR, allow_edits=False, model=get_codex_model(),
            )
        from claude_agent_sdk import (
            AssistantMessage,
            ClaudeAgentOptions,
            TextBlock,
            query,
        )

        response = ""
        async for message in query(
            prompt=prompt,
            options=ClaudeAgentOptions(
                cwd=str(ROOT_DIR),
                model=get_claude_model(),
                allowed_tools=[],
                max_turns=2,
            ),
        ):
            if isinstance(message, AssistantMessage):
                for block in message.content:
                    if isinstance(block, TextBlock):
                        response += block.text
        return response


async def run_summary_rewrite(batch_size: int = BATCH_SIZE) -> int:
    """Rewrite all over-long/stub index summaries. Returns rows changed.

    Best-effort by contract: an LLM failure logs a warning and returns what
    was already applied — callers (post-compile) must never fail because of
    this pass.
    """
    targets = collect_rewrite_targets()
    if not targets:
        return 0
    print(f"  Index summary rewrite: {len(targets)} target(s)")
    total = 0
    for start in range(0, len(targets), batch_size):
        batch = targets[start:start + batch_size]
        try:
            response = await _call_llm(build_rewrite_prompt(batch))
        except Exception as e:
            print(f"  Warning: summary rewrite LLM call failed: {e}")
            break
        applied = apply_rewrites(parse_rewrite_response(response, batch))
        total += applied
        print(f"  Batch {start // batch_size + 1}: {applied}/{len(batch)} rewritten")
    return total


def main() -> int:
    parser = argparse.ArgumentParser(description="Rewrite bloated index summaries")
    parser.add_argument("--dry-run", action="store_true",
                        help="List rewrite targets without calling the LLM")
    args = parser.parse_args()

    targets = collect_rewrite_targets()
    if args.dry_run:
        for t in targets:
            print(f"{t['kind']:5} {t['target']} ({len(t['summary'])} chars)")
        print(f"{len(targets)} target(s)")
        return 0

    changed = asyncio.run(run_summary_rewrite())
    print(f"Rewrote {changed} index summaries")
    if changed:
        from kb_git import ensure_kb_repo, kb_commit

        ensure_kb_repo()
        kb_commit(f"index summary rewrite ({changed} rows)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 6: Run to verify pass** — `uv run python -m pytest tests/test_index_rewrite.py tests/test_lint.py -v` → PASS
- [ ] **Step 7: Commit** — `git commit -m "feat: batched LLM rewrite of bloated index summaries and stub rows"`

---

### Task 5: Wire rewrite into compile + maintenance

**Files:**
- Modify: `scripts/compile.py` (replace `run_summary_rewrite_best_effort` placeholder), `scripts/maintenance.py`
- Test: covered by Task 2's tests (placeholder already patched) + one maintenance assertion

- [ ] **Step 1: Replace the placeholder in compile.py**

```python
def run_summary_rewrite_best_effort() -> None:
    """Post-compile index summary rewrite; must never fail the compile."""
    try:
        from index_rewrite import run_summary_rewrite

        asyncio.run(run_summary_rewrite())
    except Exception as e:
        print(f"  Warning: index summary rewrite skipped: {e}")
```

- [ ] **Step 2: Add maintenance step** in `scripts/maintenance.py` after the `lint-fix` step:

```python
    # Post-compile covers the normal path; this drains anything left over.
    # At 04:30 a locked keychain makes the LLM call fail harmlessly.
    run_step("index-rewrite", uv + [str(SCRIPTS_DIR / "index_rewrite.py")])
```

- [ ] **Step 3: Run full suite** — `uv run python -m pytest` → PASS
- [ ] **Step 4: Commit** — `git commit -m "feat: run index summary rewrite post-compile and in nightly maintenance"`

---

### Task 6: `consolidate` module — monthly fold-thin-articles pass

**Files:**
- Create: `scripts/consolidate.py`
- Test: `tests/test_consolidate.py`

**Interfaces:**
- Consumes: `kb_git.ensure_kb_repo/kb_commit/kb_rollback`, `utils.INDEX_ROW_RE/count_inbound_links/get_article_word_count/list_wiki_articles/read_wiki_index/update_state`, `compile.get_compile_timeout_seconds` (top-level import here; compile imports consolidate only lazily inside a function, so no cycle).
- Produces: `select_candidates(max_candidates=15) -> list[dict]` (`{target, words, updated}`), `build_consolidation_prompt(candidates) -> str`, `apply_manifest() -> list[str]`, `async run_consolidation() -> bool`, CLI with `--dry-run`. Deletion protocol: LLM appends `DELETE concepts/foo` lines to `reports/consolidate-manifest.txt`; script deletes file + index row only when inbound links are zero. On structural errors after the pass: `kb_rollback()`. Success records `state["last_consolidation"] = now_iso()`.

- [ ] **Step 1: Write failing tests** (`tests/test_consolidate.py`)

```python
from __future__ import annotations

import asyncio
from pathlib import Path

import consolidate


HEADER = "# Index\n\n| Article | Summary | Compiled From | Updated |\n|---|---|---|---|\n"


def _setup_kb(monkeypatch, tmp_path: Path) -> Path:
    knowledge_dir = tmp_path / "knowledge"
    concepts = knowledge_dir / "concepts"
    concepts.mkdir(parents=True)
    monkeypatch.setattr(consolidate, "KNOWLEDGE_DIR", knowledge_dir)
    monkeypatch.setattr(consolidate, "INDEX_FILE", knowledge_dir / "index.md")
    monkeypatch.setattr(consolidate, "MANIFEST_FILE", tmp_path / "manifest.txt")
    monkeypatch.setattr(
        consolidate, "list_wiki_articles", lambda: sorted(concepts.glob("*.md"))
    )
    return knowledge_dir


def test_select_candidates_sparse_and_old_only(monkeypatch, tmp_path: Path) -> None:
    kb = _setup_kb(monkeypatch, tmp_path)
    (kb / "concepts" / "thin-old.md").write_text("few words", encoding="utf-8")
    (kb / "concepts" / "thin-fresh.md").write_text("few words", encoding="utf-8")
    (kb / "concepts" / "big-old.md").write_text("word " * 300, encoding="utf-8")
    (kb / "index.md").write_text(HEADER + "\n".join([
        "| [[concepts/thin-old]] | a | daily/a.md | 2026-01-01 |",
        "| [[concepts/thin-fresh]] | b | daily/b.md | 2099-01-01 |",
        "| [[concepts/big-old]] | c | daily/c.md | 2026-01-01 |",
    ]) + "\n", encoding="utf-8")

    targets = [c["target"] for c in consolidate.select_candidates()]

    assert targets == ["concepts/thin-old"]


def test_apply_manifest_deletes_file_and_index_row(monkeypatch, tmp_path: Path) -> None:
    kb = _setup_kb(monkeypatch, tmp_path)
    (kb / "concepts" / "gone.md").write_text("thin", encoding="utf-8")
    (kb / "concepts" / "hub.md").write_text("no links here", encoding="utf-8")
    (kb / "index.md").write_text(HEADER + "\n".join([
        "| [[concepts/gone]] | thin | daily/a.md | 2026-01-01 |",
        "| [[concepts/hub]] | hub | daily/b.md | 2026-01-01 |",
    ]) + "\n", encoding="utf-8")
    consolidate.MANIFEST_FILE.write_text(
        "DELETE concepts/gone\nDELETE ../etc/passwd\nnoise\n", encoding="utf-8"
    )

    deleted = consolidate.apply_manifest()

    assert deleted == ["concepts/gone"]
    assert not (kb / "concepts" / "gone.md").exists()
    index_text = (kb / "index.md").read_text(encoding="utf-8")
    assert "concepts/gone" not in index_text
    assert "[[concepts/hub]]" in index_text
    assert not consolidate.MANIFEST_FILE.exists()


def test_apply_manifest_skips_articles_with_inbound_links(monkeypatch, tmp_path: Path) -> None:
    kb = _setup_kb(monkeypatch, tmp_path)
    (kb / "concepts" / "linked.md").write_text("thin", encoding="utf-8")
    (kb / "concepts" / "hub.md").write_text("see [[concepts/linked]]", encoding="utf-8")
    (kb / "index.md").write_text(
        HEADER + "| [[concepts/linked]] | thin | daily/a.md | 2026-01-01 |\n",
        encoding="utf-8",
    )
    consolidate.MANIFEST_FILE.write_text("DELETE concepts/linked", encoding="utf-8")

    assert consolidate.apply_manifest() == []
    assert (kb / "concepts" / "linked.md").exists()


def test_run_consolidation_rolls_back_on_llm_failure(monkeypatch, tmp_path: Path) -> None:
    kb = _setup_kb(monkeypatch, tmp_path)
    (kb / "concepts" / "thin-old.md").write_text("few words", encoding="utf-8")
    (kb / "index.md").write_text(
        HEADER + "| [[concepts/thin-old]] | a | daily/a.md | 2026-01-01 |\n",
        encoding="utf-8",
    )
    calls: list[str] = []
    monkeypatch.setattr(consolidate, "ensure_kb_repo", lambda: calls.append("ensure"))
    monkeypatch.setattr(consolidate, "kb_commit", lambda msg: calls.append(f"commit:{msg}"))
    monkeypatch.setattr(consolidate, "kb_rollback", lambda: calls.append("rollback"))

    async def broken(prompt: str) -> None:
        raise RuntimeError("SDK down")

    monkeypatch.setattr(consolidate, "_run_llm_agent", broken)

    assert asyncio.run(consolidate.run_consolidation()) is False
    assert "rollback" in calls
    assert any(c.startswith("commit:checkpoint") for c in calls)
```

- [ ] **Step 2: Run to verify failure** — `uv run python -m pytest tests/test_consolidate.py -v` → FAIL

- [ ] **Step 3: Implement `scripts/consolidate.py`**

```python
"""Monthly consolidation pass: fold thin articles into hub articles.

Candidates are selected mechanically (sparse articles that stopped growing);
an LLM agent folds their content into related hubs and records deletions in
a manifest; the script applies deletions deterministically, verifies
structure, and rolls the whole pass back via the knowledge/ git repo when
verification fails. This is the "sleep" phase that keeps the KB dense as it
approaches the ~500-article scale ceiling of index-based retrieval.

Usage:
    uv run python scripts/consolidate.py             # run one pass
    uv run python scripts/consolidate.py --dry-run   # list candidates only
"""

from __future__ import annotations

import argparse
import asyncio
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

from codex_exec import run_codex_prompt
from compile import get_compile_timeout_seconds
from config import (
    AGENTS_FILE,
    INDEX_FILE,
    KNOWLEDGE_DIR,
    LLM_LOCK_FILE,
    LOCKS_DIR,
    LOG_FILE,
    REPORTS_DIR,
    now_iso,
)
from kb_git import ensure_kb_repo, kb_commit, kb_rollback
from locking import file_lock
from runtime_config import get_claude_model, get_codex_model, get_task_runtime
from utils import (
    INDEX_ROW_RE,
    count_inbound_links,
    get_article_word_count,
    list_wiki_articles,
    read_wiki_index,
    update_state,
)

ROOT_DIR = Path(__file__).resolve().parent.parent
MANIFEST_FILE = REPORTS_DIR / "consolidate-manifest.txt"
MAX_CANDIDATES = 15
SPARSE_WORDS = 200
MIN_AGE_DAYS = 14

_DELETE_LINE_RE = re.compile(r"^DELETE\s+((?:concepts|connections|qa)/[\w-]+)$")


def _index_updated_dates() -> dict[str, str]:
    dates: dict[str, str] = {}
    if not INDEX_FILE.exists():
        return dates
    for line in INDEX_FILE.read_text(encoding="utf-8").splitlines():
        m = INDEX_ROW_RE.match(line.strip())
        if m:
            dates[m.group(1)] = m.group(4)
    return dates


def select_candidates(max_candidates: int = MAX_CANDIDATES) -> list[dict]:
    """Sparse articles (<SPARSE_WORDS words) not touched in MIN_AGE_DAYS days.

    Recently-updated thin articles are excluded — they may still grow
    naturally through compiles; folding them would be premature.
    """
    cutoff = (
        datetime.now(timezone.utc).astimezone() - timedelta(days=MIN_AGE_DAYS)
    ).strftime("%Y-%m-%d")
    dates = _index_updated_dates()
    candidates = []
    for article in list_wiki_articles():
        words = get_article_word_count(article)
        if words >= SPARSE_WORDS:
            continue
        rel = str(article.relative_to(KNOWLEDGE_DIR)).replace(".md", "").replace("\\", "/")
        updated = dates.get(rel, "")
        if updated >= cutoff:
            continue
        candidates.append({"target": rel, "words": words, "updated": updated or "(unindexed)"})
    candidates.sort(key=lambda c: c["words"])
    return candidates[:max_candidates]


def build_consolidation_prompt(candidates: list[dict]) -> str:
    schema = AGENTS_FILE.read_text(encoding="utf-8")
    index = read_wiki_index()
    blocks = []
    for c in candidates:
        path = KNOWLEDGE_DIR / f"{c['target']}.md"
        blocks.append(
            f"### {c['target']} ({c['words']} words, updated {c['updated']})\n\n"
            + path.read_text(encoding="utf-8")
        )
    joined = "\n\n---\n\n".join(blocks)
    return f"""You are the consolidation pass of a knowledge-base compiler.
Thin articles accumulate over time; your job is to fold them into stronger
hub articles so the knowledge base stays dense and navigable.

## Schema (AGENTS.md)

{schema}

## Current Wiki Index

{index}

## Fold Candidates (thin articles, full content)

{joined}

## Your Task

For EACH candidate decide: FOLD into an existing, semantically related hub
article, or KEEP as-is (only if genuinely distinct and likely to keep growing).

When folding [[X]] into hub [[H]]:
1. Edit the hub article under {KNOWLEDGE_DIR}: merge X's real content (no
   filler), add X's sources to the hub's frontmatter sources list.
2. Update every article that links to [[X]] to link to [[H]] instead
   (grep the knowledge directory for the link target).
3. Update H's row in {INDEX_FILE} (summary <= 200 chars, updated = today).
   Do NOT delete X's row or X's file yourself.
4. Append one line to {MANIFEST_FILE} (create it if missing), bare target only:
   DELETE concepts/x

When keeping a candidate, change nothing about it.

Finally append ONE entry to {LOG_FILE}:
## [{now_iso()}] consolidate
- Folded: [[concepts/x]] -> [[concepts/hub]], ... (or 'none')
- Kept: [[concepts/y]] (short reason), ...

Do not create new articles. Do not touch articles unrelated to the folds."""


def _remove_index_row(target: str) -> None:
    if not INDEX_FILE.exists():
        return
    lines = INDEX_FILE.read_text(encoding="utf-8").splitlines()
    kept = [
        line for line in lines
        if not (
            (m := INDEX_ROW_RE.match(line.strip())) and m.group(1) == target
        )
    ]
    INDEX_FILE.write_text("\n".join(kept) + "\n", encoding="utf-8")


def apply_manifest() -> list[str]:
    """Delete manifest-listed articles once nothing links to them anymore.

    The LLM never deletes files itself — this keeps deletions deterministic,
    path-validated, and guarded by a link check.
    """
    if not MANIFEST_FILE.exists():
        return []
    deleted: list[str] = []
    for raw in MANIFEST_FILE.read_text(encoding="utf-8").splitlines():
        m = _DELETE_LINE_RE.match(raw.strip())
        if not m:
            continue
        target = m.group(1)
        path = KNOWLEDGE_DIR / f"{target}.md"
        if not path.exists():
            continue
        if count_inbound_links(target, exclude_file=path) > 0:
            print(f"  Skipping delete of {target}: inbound links remain")
            continue
        path.unlink()
        _remove_index_row(target)
        deleted.append(target)
    MANIFEST_FILE.unlink(missing_ok=True)
    return deleted


async def _run_llm_agent(prompt: str) -> None:
    runtime = get_task_runtime("consolidate")
    model = get_codex_model() if runtime == "codex" else get_claude_model()
    print(f"  Runtime: {runtime} (model: {model or 'default'})")
    with file_lock(LLM_LOCK_FILE):
        if runtime == "codex":
            await asyncio.to_thread(
                run_codex_prompt, prompt,
                cwd=ROOT_DIR, allow_edits=True, model=get_codex_model(),
            )
            return

        from claude_agent_sdk import ClaudeAgentOptions, ResultMessage, query

        result_subtype: str | None = None

        async def run() -> None:
            nonlocal result_subtype
            async for message in query(
                prompt=prompt,
                options=ClaudeAgentOptions(
                    cwd=str(ROOT_DIR),
                    model=get_claude_model(),
                    system_prompt={"type": "preset", "preset": "claude_code"},
                    allowed_tools=["Read", "Write", "Edit", "Glob", "Grep"],
                    permission_mode="acceptEdits",
                    max_turns=60,
                ),
            ):
                if isinstance(message, ResultMessage):
                    result_subtype = getattr(message, "subtype", None)

        try:
            async with asyncio.timeout(get_compile_timeout_seconds()):
                await run()
        except TimeoutError:
            if result_subtype != "success":
                raise RuntimeError("consolidation timed out") from None
        except Exception:
            # Stream teardown can fail AFTER a successful result (same
            # pattern as compile.py) — the work is done, don't fail the pass.
            if result_subtype != "success":
                raise
        if result_subtype and result_subtype != "success":
            raise RuntimeError(f"consolidation ended with subtype '{result_subtype}'")


def _record_consolidation() -> None:
    def mutate(state: dict) -> None:
        state["last_consolidation"] = now_iso()

    update_state(mutate)


async def run_consolidation() -> bool:
    """One consolidation pass. Returns True on success (including no-op).

    NOTE: does NOT take compile.lock — the compile trigger calls this while
    already holding it (flock re-entry from the same process would deadlock).
    The standalone CLI takes the lock in main().
    """
    candidates = select_candidates()
    if not candidates:
        print("  Consolidation: no candidates.")
        _record_consolidation()
        return True
    print(f"  Consolidation: {len(candidates)} candidate(s)")

    ensure_kb_repo()
    kb_commit("checkpoint before consolidation")
    MANIFEST_FILE.parent.mkdir(parents=True, exist_ok=True)
    MANIFEST_FILE.unlink(missing_ok=True)

    try:
        await _run_llm_agent(build_consolidation_prompt(candidates))
    except Exception as e:
        print(f"  Error: consolidation LLM run failed: {e}")
        kb_rollback()
        return False

    deleted = apply_manifest()

    from lint import check_broken_links, check_index_consistency

    errors = [
        i for i in check_broken_links() + check_index_consistency()
        if i["severity"] == "error"
    ]
    if errors:
        print(f"  Consolidation produced {len(errors)} structural error(s) — rolling back.")
        for issue in errors[:10]:
            print(f"    {issue['detail']}")
        kb_rollback()
        return False

    kb_commit(f"consolidation pass ({len(deleted)} folded)")
    _record_consolidation()
    print(f"  Consolidation complete: {len(deleted)} article(s) folded.")
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description="Fold thin articles into hubs")
    parser.add_argument("--dry-run", action="store_true",
                        help="List fold candidates without calling the LLM")
    args = parser.parse_args()

    if args.dry_run:
        for c in select_candidates():
            print(f"{c['words']:4d} words  {c['target']}  (updated {c['updated']})")
        return 0

    with file_lock(LOCKS_DIR / "compile.lock"):
        ok = asyncio.run(run_consolidation())
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 4: Run to verify pass** — `uv run python -m pytest tests/test_consolidate.py -v` → PASS
- [ ] **Step 5: Commit** — `git commit -m "feat: monthly consolidation pass folding thin articles into hubs"`

---

### Task 7: Consolidation trigger in compile.py

**Files:**
- Modify: `scripts/compile.py` (replace `maybe_run_consolidation` placeholder)
- Test: `tests/test_compile.py` (append)

- [ ] **Step 1: Write failing tests**

```python
def test_maybe_run_consolidation_respects_interval(monkeypatch) -> None:
    ran = []
    monkeypatch.setattr(
        compile_script, "load_state",
        lambda: {"last_consolidation": "2026-07-01T10:00:00+05:00"},
    )
    monkeypatch.setattr(
        compile_script, "_run_consolidation_pass", lambda: ran.append(True)
    )

    compile_script.maybe_run_consolidation()

    assert ran == []


def test_maybe_run_consolidation_runs_when_stale(monkeypatch) -> None:
    ran = []
    monkeypatch.setattr(
        compile_script, "load_state",
        lambda: {"last_consolidation": "2026-01-01T10:00:00+05:00"},
    )
    monkeypatch.setattr(
        compile_script, "_run_consolidation_pass", lambda: ran.append(True)
    )

    compile_script.maybe_run_consolidation()

    assert ran == [True]
```

- [ ] **Step 2: Run to verify failure** — `uv run python -m pytest tests/test_compile.py -v` → FAIL

- [ ] **Step 3: Implement** (replace the Task 2 placeholder in `scripts/compile.py`)

```python
CONSOLIDATION_INTERVAL_DAYS = 30


def _run_consolidation_pass() -> None:
    # Lazy import: consolidate imports compile (timeout helper), so importing
    # it at module top would be circular.
    from consolidate import run_consolidation

    ok = asyncio.run(run_consolidation())
    if not ok:
        print("  Consolidation failed; will retry on a later compile.")


def maybe_run_consolidation() -> None:
    """Run a consolidation pass when the last one is older than the interval.

    Triggered post-compile (not from 04:30 maintenance) so the LLM runs in
    active hours with an unlocked keychain, right after compile proved the
    runtime works.
    """
    from datetime import datetime, timedelta, timezone

    state = load_state()
    last = state.get("last_consolidation")
    if last:
        try:
            last_dt = datetime.fromisoformat(last)
        except ValueError:
            last_dt = None
        if last_dt is not None:
            age = datetime.now(timezone.utc).astimezone() - last_dt
            if age < timedelta(days=CONSOLIDATION_INTERVAL_DAYS):
                return
    print("\nRunning monthly consolidation pass...")
    _run_consolidation_pass()
```

- [ ] **Step 4: Run full suite** — `uv run python -m pytest` → PASS
- [ ] **Step 5: Commit** — `git commit -m "feat(compile): monthly post-compile consolidation trigger"`

---

### Task 8: MCP search scoring + read telemetry

**Files:**
- Modify: `scripts/mcp_server.py`, `.gitignore` (add `scripts/usage.json`)
- Test: Create `tests/test_mcp_server.py`

**Interfaces:**
- Produces: `USAGE_FILE = ROOT_DIR / "scripts" / "usage.json"` with shape `{"article_reads": {"concepts/x": {"count": N, "last": "YYYY-MM-DD"}}}` (keys WITHOUT `.md`, forward slashes) — consumed by Task 9. `_record_article_read(rel_path: str) -> None` best-effort. New scoring: per keyword `min(occurrences, 5)` + `5` title bonus; unmatched keywords contribute 0.

- [ ] **Step 1: Write failing tests** (`tests/test_mcp_server.py`)

```python
from __future__ import annotations

import json
from pathlib import Path

import mcp_server


def _setup(monkeypatch, tmp_path: Path) -> Path:
    knowledge_dir = tmp_path / "knowledge"
    concepts = knowledge_dir / "concepts"
    concepts.mkdir(parents=True)
    monkeypatch.setattr(mcp_server, "KNOWLEDGE_DIR", knowledge_dir)
    monkeypatch.setattr(mcp_server, "ARTICLE_DIRS", [concepts])
    monkeypatch.setattr(mcp_server, "USAGE_FILE", tmp_path / "usage.json")
    monkeypatch.setattr(mcp_server, "USAGE_LOCK", tmp_path / "usage.lock")
    return concepts


def test_search_ranks_title_match_above_body_mention(monkeypatch, tmp_path: Path) -> None:
    concepts = _setup(monkeypatch, tmp_path)
    (concepts / "docker-guide.md").write_text(
        '---\ntitle: "Docker Guide"\n---\n\nAbout docker.\n', encoding="utf-8"
    )
    (concepts / "misc.md").write_text(
        '---\ntitle: "Misc"\n---\n\nMentions docker once.\n', encoding="utf-8"
    )

    result = mcp_server.search_knowledge("docker")

    assert result.index("docker-guide") < result.index("misc.md")


def test_read_article_records_usage(monkeypatch, tmp_path: Path) -> None:
    concepts = _setup(monkeypatch, tmp_path)
    (concepts / "topic.md").write_text("---\ntitle: T\n---\n\nBody.\n", encoding="utf-8")

    mcp_server.read_article("concepts/topic")
    mcp_server.read_article("concepts/topic.md")

    data = json.loads((tmp_path / "usage.json").read_text(encoding="utf-8"))
    assert data["article_reads"]["concepts/topic"]["count"] == 2


def test_usage_recording_survives_corrupt_file(monkeypatch, tmp_path: Path) -> None:
    concepts = _setup(monkeypatch, tmp_path)
    (concepts / "topic.md").write_text("Body.", encoding="utf-8")
    (tmp_path / "usage.json").write_text("{corrupt", encoding="utf-8")

    assert "Body." in mcp_server.read_article("concepts/topic")
    data = json.loads((tmp_path / "usage.json").read_text(encoding="utf-8"))
    assert data["article_reads"]["concepts/topic"]["count"] == 1
```

- [ ] **Step 2: Run to verify failure** — `uv run python -m pytest tests/test_mcp_server.py -v` → FAIL

- [ ] **Step 3: Implement in `scripts/mcp_server.py`.** Add imports (`import json`, `from datetime import datetime, timezone`, `from locking import file_lock`) and constants after `DAILY_DIR`:

```python
USAGE_FILE = ROOT_DIR / "scripts" / "usage.json"
USAGE_LOCK = ROOT_DIR / "scripts" / ".locks" / "usage.lock"


def _record_article_read(rel_path: str) -> None:
    """Best-effort read counter feeding session-start hub selection.

    Telemetry must never break the tool — any failure is swallowed.
    """
    try:
        with file_lock(USAGE_LOCK):
            data: dict = {}
            if USAGE_FILE.exists():
                try:
                    data = json.loads(USAGE_FILE.read_text(encoding="utf-8"))
                except json.JSONDecodeError:
                    data = {}
            if not isinstance(data, dict):
                data = {}
            reads = data.setdefault("article_reads", {})
            entry = reads.setdefault(rel_path, {"count": 0})
            entry["count"] = int(entry.get("count", 0)) + 1
            entry["last"] = datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d")
            USAGE_FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")
    except Exception:
        pass
```

Replace the scoring block inside `search_knowledge` (extract title BEFORE scoring):

```python
    for article in _list_articles():
        content = article.read_text(encoding="utf-8")
        content_lower = content.lower()
        rel = article.relative_to(KNOWLEDGE_DIR)

        title = str(rel)
        title_match = re.search(r"^title:\s*(.+)$", content, re.MULTILINE)
        if title_match:
            title = title_match.group(1).strip().strip('"')
        title_lower = title.lower()

        # Score: capped keyword frequency + a strong title-hit bonus.
        score = 0
        matched = 0
        for kw in keywords:
            occurrences = content_lower.count(kw)
            if occurrences == 0:
                continue
            matched += 1
            score += min(occurrences, 5)
            if kw in title_lower:
                score += 5
        if matched == 0:
            continue
```

and update the relevance line to `**Relevance:** {matched}/{len(keywords)} keywords (score {score})`.

In `read_article`, record reads on both success paths (exact and fuzzy):

```python
        # fuzzy branch:
            if slug in a.stem:
                _record_article_read(
                    str(a.relative_to(KNOWLEDGE_DIR)).removesuffix(".md").replace("\\", "/")
                )
                return f"(Did you mean {a.relative_to(KNOWLEDGE_DIR)}?)\n\n" + a.read_text(encoding="utf-8")
    # exact:
    _record_article_read(
        str(article.relative_to(KNOWLEDGE_DIR)).removesuffix(".md").replace("\\", "/")
    )
    return article.read_text(encoding="utf-8")
```

Add `scripts/usage.json` to `.gitignore` under "Runtime state".

- [ ] **Step 4: Run to verify pass** — `uv run python -m pytest tests/test_mcp_server.py -v` → PASS
- [ ] **Step 5: Commit** — `git commit -m "feat(mcp): frequency+title search scoring and article read telemetry"`

---

### Task 9: Usage-aware hub selection in session-start

**Files:**
- Modify: `hooks/session-start.py` (stdlib-only!)
- Test: `tests/test_session_start.py` (append)

**Interfaces:**
- Consumes: `scripts/usage.json` from Task 8 (reads it directly with json — no scripts/ imports).
- Produces: `load_usage_counts() -> dict[str, int]`; `select_tier_rows(..., usage: dict | None = None)` — hubs ranked by `(-reads, -source_count, link)`, eligibility `source_count >= 2 OR reads >= 2`; `build_kb_section(rows, now, budget, usage=None)` passes it through.

- [ ] **Step 1: Write failing tests** (append; module loaded via existing `load_session_start_module()`)

```python
def test_select_tier_hubs_boosted_by_usage():
    mod = load_session_start_module()
    rows = mod.parse_index_rows(SAMPLE_INDEX)
    usage = {"concepts/old-single": 7}

    recent, hubs = mod.select_tier_rows(rows, NOW, usage=usage)

    hub_links = [r["link"] for r in hubs]
    # single-source article qualifies via reads and outranks source-count hubs
    assert hub_links[0] == "[[concepts/old-single]]"
    assert "[[concepts/old-hub]]" in hub_links


def test_select_tier_without_usage_unchanged():
    mod = load_session_start_module()
    rows = mod.parse_index_rows(SAMPLE_INDEX)

    _, hubs = mod.select_tier_rows(rows, NOW)

    assert [r["link"] for r in hubs] == [
        "[[concepts/old-hub]]", "[[concepts/old-pair]]"
    ]


def test_load_usage_counts_missing_and_corrupt(tmp_path, monkeypatch):
    mod = load_session_start_module()
    monkeypatch.setattr(mod, "USAGE_FILE", tmp_path / "missing.json")
    assert mod.load_usage_counts() == {}

    corrupt = tmp_path / "usage.json"
    corrupt.write_text("{nope", encoding="utf-8")
    monkeypatch.setattr(mod, "USAGE_FILE", corrupt)
    assert mod.load_usage_counts() == {}

    good = tmp_path / "good.json"
    good.write_text(
        '{"article_reads": {"concepts/x": {"count": 3, "last": "2026-07-01"}}}',
        encoding="utf-8",
    )
    monkeypatch.setattr(mod, "USAGE_FILE", good)
    assert mod.load_usage_counts() == {"concepts/x": 3}
```

- [ ] **Step 2: Run to verify failure** — `uv run python -m pytest tests/test_session_start.py -v` → FAIL

- [ ] **Step 3: Implement in `hooks/session-start.py`.** Add after `INDEX_FILE`:

```python
USAGE_FILE = ROOT / "scripts" / "usage.json"
MIN_HUB_READS = 2


def load_usage_counts() -> dict:
    """Article read counters written by the MCP server; {} when absent/corrupt."""
    try:
        data = json.loads(USAGE_FILE.read_text(encoding="utf-8"))
        reads = data.get("article_reads", {})
        return {
            key: int(value.get("count", 0))
            for key, value in reads.items()
            if isinstance(value, dict)
        }
    except (OSError, ValueError, AttributeError):
        return {}
```

Change `select_tier_rows` signature to `(rows, now, recent_days=RECENT_DAYS, max_hubs=MAX_HUB_ROWS, usage=None)` and the hub block to:

```python
    usage = usage or {}

    def reads(row: dict) -> int:
        return usage.get(row["link"].strip("[]"), 0)

    remaining = [r for r in rows if r["link"] not in recent_links]
    remaining.sort(key=lambda r: (-reads(r), -r["source_count"], r["link"]))
    hubs = [
        r for r in remaining
        if r["source_count"] >= 2 or reads(r) >= MIN_HUB_READS
    ][:max_hubs]
```

Change `build_kb_section(rows, now, budget, usage=None)` → pass `usage=usage` to `select_tier_rows`; in `build_context` call `build_kb_section(rows, now, budget, load_usage_counts())`.

- [ ] **Step 4: Run to verify pass** — `uv run python -m pytest tests/test_session_start.py -v` → PASS
- [ ] **Step 5: Commit** — `git commit -m "feat(session-start): blend MCP read telemetry into hub selection"`

---

### Task 10: Docs, backfill, end-to-end verification

**Files:**
- Modify: `docs/operations.md`, `README.md` (Key Commands)

- [ ] **Step 1: Document in `docs/operations.md`** — add sections:
  - **Knowledge Base Versioning**: nested git repo in `knowledge/` (auto-created on first compile), per-log checkpoint/commit, rollback on failure, inflight marker semantics (`scripts/.locks/compile-inflight.json`), `git -C knowledge log --oneline` to audit what each compile changed, caveat: rollback after kill -9 also discards any lint fixes made between the crash and the next compile (mechanical, regenerated).
  - **Index Summary Rewrite**: what triggers it (post-compile + nightly maintenance + manual `uv run python scripts/index_rewrite.py [--dry-run]`), batched `target: summary` protocol, best-effort contract.
  - **Consolidation Pass**: monthly post-compile trigger (`last_consolidation` in state.json), candidate rule (<200 words, untouched 14 days), manifest-based deletion with inbound-link guard, git rollback on structural errors, manual run `uv run python scripts/consolidate.py [--dry-run]`.
  - One line under Session-Start Context Budget: hub selection now also weighs `scripts/usage.json` read counters.
- [ ] **Step 2: README Key Commands** — add `index_rewrite.py --dry-run` and `consolidate.py --dry-run` lines.
- [ ] **Step 3: Full suite** — `uv run python -m pytest` → all PASS.
- [ ] **Step 4: Initialize the real KB repo** — `uv run python -c "import sys; sys.path.insert(0, 'scripts'); import kb_git; print(kb_git.ensure_kb_repo())"` → True; verify `git -C knowledge log --oneline` shows the init snapshot.
- [ ] **Step 5: Backfill summaries** — `uv run python scripts/index_rewrite.py --dry-run` (expect ~130 targets incl. 2 stubs), then real run (3 batches, LLM); verify `uv run python scripts/lint.py --structural-only` suggestions drop to ~1; check `git -C knowledge log` has the rewrite commit.
- [ ] **Step 6: Consolidation dry-run only** — `uv run python scripts/consolidate.py --dry-run` to show candidates; the first real (LLM, deleting) pass is run supervised by the user.
- [ ] **Step 7: Commit docs** — `git commit -m "docs: operations for kb versioning, summary rewrite, consolidation"`

## Self-Review Notes

- Spec coverage: 1A→Tasks 1-2, 2A→Tasks 4-5, 4→Tasks 6-7, 5→Tasks 8-9 (auto-stub enforcement lives in Task 4's STUB_MARKER targets). Docs/backfill→Task 10.
- Circular import compile↔consolidate resolved: consolidate imports compile top-level; compile imports consolidate only inside `_run_consolidation_pass`.
- flock re-entry deadlock avoided: `run_consolidation()` never takes compile.lock; only the standalone CLI does.
- Task 2's tests patch the Task 5/7 placeholders, so the suite is green at every task boundary.
