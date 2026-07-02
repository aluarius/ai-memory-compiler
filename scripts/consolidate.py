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
    extract_wikilinks,
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


def _inbound_links_exist(target: str, exclude_file: Path) -> bool:
    for article in list_wiki_articles():
        if article == exclude_file:
            continue
        if target in extract_wikilinks(article.read_text(encoding="utf-8")):
            return True
    return False


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
        if _inbound_links_exist(target, exclude_file=path):
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
