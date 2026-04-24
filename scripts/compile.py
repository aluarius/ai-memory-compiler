"""
Compile daily conversation logs into structured knowledge articles.

This is the "LLM compiler" - it reads daily logs (source code) and produces
organized knowledge articles (the executable).

Usage:
    uv run python compile.py                    # compile new/changed logs only
    uv run python compile.py --all              # force recompile everything
    uv run python compile.py --file daily/2026-04-01.md  # compile a specific log
    uv run python compile.py --dry-run          # show what would be compiled
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

from codex_exec import run_codex_prompt
from config import (
    AGENTS_FILE,
    CONCEPTS_DIR,
    CONNECTIONS_DIR,
    DAILY_DIR,
    DAILY_LOG_LOCK_FILE,
    KNOWLEDGE_DIR,
    LOCKS_DIR,
    now_iso,
)
from locking import file_lock
from runtime_config import get_codex_model, get_task_runtime
from utils import (
    file_hash,
    list_raw_files,
    list_wiki_articles,
    load_state,
    normalize_build_log_file,
    read_wiki_index,
    update_state,
)

# ── Paths for the LLM to use ──────────────────────────────────────────
ROOT_DIR = Path(__file__).resolve().parent.parent


async def compile_daily_log(log_path: Path) -> float:
    """Compile a single daily log into knowledge articles.

    Returns the API cost of the compilation.
    """
    with file_lock(DAILY_LOG_LOCK_FILE):
        log_content = log_path.read_text(encoding="utf-8")
        compiled_hash = file_hash(log_path)

    schema = AGENTS_FILE.read_text(encoding="utf-8")
    wiki_index = read_wiki_index()

    timestamp = now_iso()

    prompt = f"""You are a knowledge compiler. Your job is to read a daily conversation log
and extract knowledge into structured wiki articles.

## Schema (AGENTS.md)

{schema}

## Current Wiki Index

{wiki_index}

## Daily Log to Compile

**File:** {log_path.name}

{log_content}

## Your Task

Read the daily log above and compile it into wiki articles following the schema exactly.

### Rules:

1. **Extract key concepts** - Identify the distinct concepts worth persisting from this log
2. **Create concept articles** in `knowledge/concepts/` - One .md file per concept
   - Use the exact article format from AGENTS.md (YAML frontmatter + sections)
   - Include `sources:` in frontmatter pointing to the daily log file
   - Use `[[concepts/slug]]` wikilinks to link to related concepts
   - Write in encyclopedia style - neutral, comprehensive
3. **Create connection articles** in `knowledge/connections/` if this log reveals non-obvious
   relationships between 2+ existing concepts
4. **Update existing articles** if this log adds new information to concepts already in the wiki
   - Read the existing article, add the new information, add the source to frontmatter
   - Use the index below to decide which existing articles to open with tools before editing
5. **Update knowledge/index.md** - Add new entries to the table
   - Each entry: `| [[path/slug]] | One-line summary | source-file | {timestamp[:10]} |`
6. **Append to knowledge/log.md** - Add a timestamped entry:
   ```
   ## [{timestamp}] compile | {log_path.name}
   - Source: daily/{log_path.name}
   - Articles created: [[concepts/x]], [[concepts/y]]
   - Articles updated: [[concepts/z]] (if any)
   ```

### File paths:
- Write concept articles to: {CONCEPTS_DIR}
- Write connection articles to: {CONNECTIONS_DIR}
- Update index at: {KNOWLEDGE_DIR / 'index.md'}
- Append log at: {KNOWLEDGE_DIR / 'log.md'}

### Quality standards:
- Every article must have complete YAML frontmatter
- Use `[[wikilinks]]` only when the relationship is genuinely meaningful
- Prefer 0-3 strong related links over invented cross-topic links
- Key Points section should have 3-5 bullet points
- Details section should have 2+ paragraphs
- Related Concepts can be short if the topic is genuinely narrow
- Sources section should cite the daily log with specific claims extracted
- Do not update unrelated articles only to manufacture backlinks
"""

    cost = 0.0
    runtime = get_task_runtime("compile")
    print(f"  Runtime: {runtime}")

    if runtime == "codex":
        try:
            await asyncio.to_thread(
                run_codex_prompt,
                prompt,
                cwd=ROOT_DIR,
                allow_edits=True,
                model=get_codex_model(),
            )
        except Exception as e:
            print(f"  Error: {e}")
            return 0.0
    else:
        from claude_agent_sdk import (
            AssistantMessage,
            ClaudeAgentOptions,
            ResultMessage,
            TextBlock,
            query,
        )

        try:
            async for message in query(
                prompt=prompt,
                options=ClaudeAgentOptions(
                    cwd=str(ROOT_DIR),
                    system_prompt={"type": "preset", "preset": "claude_code"},
                    allowed_tools=["Read", "Write", "Edit", "Glob", "Grep"],
                    permission_mode="acceptEdits",
                    max_turns=30,
                ),
            ):
                if isinstance(message, AssistantMessage):
                    for block in message.content:
                        if isinstance(block, TextBlock):
                            pass
                elif isinstance(message, ResultMessage):
                    cost = message.total_cost_usd or 0.0
                    print(f"  Cost: ${cost:.4f}")
        except Exception as e:
            print(f"  Error: {e}")
            return 0.0

    # Update state
    rel_path = log_path.name

    def mutate(current_state: dict) -> None:
        current_state.setdefault("ingested", {})[rel_path] = {
            "hash": compiled_hash,
            "compiled_at": now_iso(),
            "cost_usd": cost,
            "processor_runtime": runtime,
        }
        current_state["total_cost"] = current_state.get("total_cost", 0.0) + cost

    update_state(mutate)

    with file_lock(DAILY_LOG_LOCK_FILE):
        current_hash = file_hash(log_path)
    if current_hash != compiled_hash:
        print("  Notice: source log changed during compile; it will be recompiled on the next run.")

    normalize_build_log_file()

    return cost


def main():
    parser = argparse.ArgumentParser(description="Compile daily logs into knowledge articles")
    parser.add_argument("--all", action="store_true", help="Force recompile all logs")
    parser.add_argument("--file", type=str, help="Compile a specific daily log file")
    parser.add_argument("--dry-run", action="store_true", help="Show what would be compiled")
    args = parser.parse_args()

    with file_lock(LOCKS_DIR / "compile.lock"):
        state = load_state()

        # Determine which files to compile
        if args.file:
            target = Path(args.file)
            if not target.is_absolute():
                target = DAILY_DIR / target.name
            if not target.exists():
                # Try resolving relative to project root
                target = ROOT_DIR / args.file
            if not target.exists():
                print(f"Error: {args.file} not found")
                sys.exit(1)
            to_compile = [target]
        else:
            all_logs = list_raw_files()
            if args.all:
                to_compile = all_logs
            else:
                to_compile = []
                for log_path in all_logs:
                    rel = log_path.name
                    prev = state.get("ingested", {}).get(rel, {})
                    if not prev or prev.get("hash") != file_hash(log_path):
                        to_compile.append(log_path)

        if not to_compile:
            print("Nothing to compile - all daily logs are up to date.")
            return

        print(f"{'[DRY RUN] ' if args.dry_run else ''}Files to compile ({len(to_compile)}):")
        for f in to_compile:
            print(f"  - {f.name}")

        if args.dry_run:
            return

        total_cost = 0.0
        for i, log_path in enumerate(to_compile, 1):
            print(f"\n[{i}/{len(to_compile)}] Compiling {log_path.name}...")
            cost = asyncio.run(compile_daily_log(log_path))
            total_cost += cost
            print("  Done.")

        articles = list_wiki_articles()
        print(f"\nCompilation complete. Total cost: ${total_cost:.2f}")
        print(f"Knowledge base: {len(articles)} articles")

        run_post_compile_lint()
        archive_old_logs()


def run_post_compile_lint() -> None:
    """Run structural lint checks after compilation."""
    from lint import (
        check_broken_links,
        check_index_consistency,
        check_orphan_pages,
        check_sparse_articles,
        check_stale_articles,
    )

    print("\nRunning post-compile health checks...")
    issues = []
    for name, fn in [
        ("Broken links", check_broken_links),
        ("Index consistency", check_index_consistency),
        ("Orphan pages", check_orphan_pages),
        ("Sparse articles", check_sparse_articles),
        ("Stale articles", check_stale_articles),
    ]:
        found = fn()
        issues.extend(found)
        if found:
            print(f"  [{name}] {len(found)} issue(s)")

    if not issues:
        print("  All checks passed.")
    else:
        errors = sum(1 for i in issues if i["severity"] == "error")
        print(f"  Total: {len(issues)} issues ({errors} errors)")


ARCHIVE_AFTER_DAYS = 30


def archive_old_logs() -> None:
    """Move compiled daily logs older than ARCHIVE_AFTER_DAYS to daily/archive/."""
    from datetime import datetime, timedelta, timezone

    cutoff = datetime.now(timezone.utc).astimezone() - timedelta(days=ARCHIVE_AFTER_DAYS)
    state = load_state()
    ingested = state.get("ingested", {})
    archive_dir = DAILY_DIR / "archive"

    for log_path in list_raw_files():
        # Only archive if already compiled
        if log_path.name not in ingested:
            continue

        # Parse date from filename (YYYY-MM-DD.md)
        try:
            log_date = datetime.strptime(log_path.stem, "%Y-%m-%d").replace(
                tzinfo=timezone.utc
            )
        except ValueError:
            continue

        if log_date < cutoff:
            archive_dir.mkdir(parents=True, exist_ok=True)
            dest = archive_dir / log_path.name
            log_path.rename(dest)
            rewrite_archived_source_refs(log_path.name)
            print(f"  Archived: {log_path.name}")


def rewrite_archived_source_refs(log_name: str) -> None:
    """Update wikilinks/frontmatter to archived daily log paths."""
    stem = Path(log_name).stem
    replacements = {
        f"[[daily/{stem}]]": f"[[daily/archive/{stem}]]",
        f'"daily/{log_name}"': f'"daily/archive/{log_name}"',
        f"'daily/{log_name}'": f"'daily/archive/{log_name}'",
        f"daily/{log_name}": f"daily/archive/{log_name}",
    }

    for md_file in KNOWLEDGE_DIR.rglob("*.md"):
        content = md_file.read_text(encoding="utf-8")
        updated = content
        for old, new in replacements.items():
            updated = updated.replace(old, new)
        if updated != content:
            md_file.write_text(updated, encoding="utf-8")


if __name__ == "__main__":
    main()
