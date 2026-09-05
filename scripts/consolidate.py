"""Monthly consolidation pass: merge or cross-link overlapping articles.

Candidates are mutually-nearest article pairs from the FTS index (see
kb_db.find_similar_pairs); an LLM agent decides FOLD / LINK / KEEP per pair,
records merges in a deletion manifest, and the script applies deletions
deterministically, verifies structure, and rolls the whole pass back via the
knowledge/ git repo when verification fails. This is the "sleep" phase that
keeps the KB dense as it approaches the ~500-article scale ceiling of
index-based retrieval.

The original sparse-article rule was dead on arrival: the compiler schema
mandates 3-5 key points and 2+ detail paragraphs, so nothing ever lands
under a 200-word threshold (observed minimum: 214 words across 449
articles). Topic overlap, not article thinness, is what actually
accumulates.

Usage:
    uv run python scripts/consolidate.py             # run one pass
    uv run python scripts/consolidate.py --dry-run   # list candidates only
"""

from __future__ import annotations

import argparse
import asyncio
import re
from pathlib import Path

import kb_db
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
from memory_store import MemoryStore
from migration_gate import guard_legacy_writer
from runtime_config import get_claude_model, get_codex_model, get_task_runtime
from utils import (
    INDEX_ROW_RE,
    extract_wikilinks,
    list_wiki_articles,
    read_wiki_index,
    update_state,
)

ROOT_DIR = Path(__file__).resolve().parent.parent
MANIFEST_FILE = REPORTS_DIR / "consolidate-manifest.txt"
MAX_CANDIDATES = 12

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
    """Overlapping article pairs from the FTS index, strongest overlap first.

    Empty (a no-op pass) when the index is unusable or nothing overlaps —
    consolidation must never invent work.
    """
    pairs = kb_db.find_similar_pairs(limit=max_candidates * 2)
    if not pairs:
        return []
    if MemoryStore.is_initialized(KNOWLEDGE_DIR.parent):
        articles = {row["path"]: row for row in MemoryStore(KNOWLEDGE_DIR.parent).list_articles()}
        return [{**pair, "updated_a": articles[pair["a"]]["updated"],
                 "updated_b": articles[pair["b"]]["updated"]}
                for pair in pairs if pair["a"] in articles and pair["b"] in articles][:max_candidates]
    dates = _index_updated_dates()
    candidates = []
    for pair in pairs:
        a, b = pair["a"], pair["b"]
        if not all((KNOWLEDGE_DIR / f"{p}.md").exists() for p in (a, b)):
            continue
        candidates.append({
            "a": a,
            "b": b,
            "score": pair["score"],
            "linked": pair["linked"],
            "updated_a": dates.get(a, "(unindexed)"),
            "updated_b": dates.get(b, "(unindexed)"),
        })
        if len(candidates) >= max_candidates:
            break
    return candidates


def _pair_block(c: dict) -> str:
    parts = [
        f"### PAIR (overlap score {c['score']}, "
        f"already cross-linked: {'yes' if c['linked'] else 'no'})"
    ]
    for side, updated in ((c["a"], c["updated_a"]), (c["b"], c["updated_b"])):
        body = (KNOWLEDGE_DIR / f"{side}.md").read_text(encoding="utf-8")
        parts.append(f"#### {side} (updated {updated})\n\n{body}")
    return "\n\n".join(parts)


def build_consolidation_prompt(candidates: list[dict]) -> str:
    schema = AGENTS_FILE.read_text(encoding="utf-8")
    index = read_wiki_index()
    joined = "\n\n---\n\n".join(_pair_block(c) for c in candidates)
    return f"""You are the consolidation pass of a knowledge-base compiler.
The pairs below were flagged as overlapping by full-text similarity. Your job
is to keep the knowledge base dense and navigable: genuinely duplicated topics
should become one article, genuinely distinct-but-related ones should at least
know about each other.

## Schema (AGENTS.md)

{schema}

## Current Wiki Index

{index}

## Overlapping Pairs (full content of both sides)

{joined}

## Your Task

For EACH pair choose exactly one verdict:

- **FOLD** — the two cover the same subject; merge the weaker/narrower one
  into the stronger one.
- **LINK** — related but genuinely distinct subjects that do not reference
  each other; add one meaningful Related Concepts link in each direction
  (skip if they are already cross-linked — then it is KEEP).
- **KEEP** — distinct enough and already discoverable; change nothing.

Prefer KEEP over LINK and LINK over FOLD when uncertain: an unnecessary merge
destroys detail, while a missing merge only costs some redundancy.

When FOLDing [[X]] into [[H]]:
1. Edit [[H]] under {KNOWLEDGE_DIR}: merge X's real content (no filler), add
   X's sources to H's frontmatter sources list.
2. Update every article that links to [[X]] to link to [[H]] instead
   (grep the knowledge directory for the link target).
3. Update H's row in {INDEX_FILE} (summary <= 200 chars, updated = today).
   Do NOT delete X's row or X's file yourself.
4. Append one line to {MANIFEST_FILE} (create it if missing), bare target only:
   DELETE concepts/x

Finally append ONE entry to {LOG_FILE}:
## [{now_iso()}] consolidate
- Folded: [[concepts/x]] -> [[concepts/hub]], ... (or 'none')
- Linked: [[concepts/a]] <-> [[concepts/b]], ... (or 'none')
- Kept: [[concepts/y]] (short reason), ...

Do not create new articles. Do not touch articles outside the pairs above."""


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


@guard_legacy_writer(lambda: KNOWLEDGE_DIR.parent)
async def run_consolidation() -> bool:
    """One consolidation pass. Returns True on success (including no-op).

    NOTE: does NOT take compile.lock — the compile trigger calls this while
    already holding it (flock re-entry from the same process would deadlock).
    The standalone CLI takes the lock in main().
    """
    if MemoryStore.is_initialized(KNOWLEDGE_DIR.parent):
        from compiler_service import consolidate_articles

        try:
            return await consolidate_articles(MemoryStore(KNOWLEDGE_DIR.parent), select_candidates())
        except (OSError, RuntimeError, ValueError) as exc:
            print(f"Error: canonical consolidation failed: {exc}")
            return False
    candidates = select_candidates()
    if not candidates:
        print("  Consolidation: no overlapping pairs.")
        _record_consolidation()
        return True
    print(f"  Consolidation: {len(candidates)} overlapping pair(s)")

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


@guard_legacy_writer(lambda: KNOWLEDGE_DIR.parent)
def main() -> int:
    parser = argparse.ArgumentParser(description="Merge or cross-link overlapping articles")
    parser.add_argument("--dry-run", action="store_true",
                        help="List overlapping pairs without calling the LLM")
    args = parser.parse_args()

    if args.dry_run:
        candidates = select_candidates()
        for c in candidates:
            link = "linked" if c["linked"] else "UNLINKED"
            print(f"{c['score']:5.2f}  {link:8}  {c['a']}  <>  {c['b']}")
        print(f"{len(candidates)} overlapping pair(s)")
        return 0

    with file_lock(LOCKS_DIR / "compile.lock"):
        ok = asyncio.run(run_consolidation())
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
