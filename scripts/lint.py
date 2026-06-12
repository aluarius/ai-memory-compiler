"""
Lint the knowledge base for structural and semantic health.

Runs structural checks for broken links, index consistency, orphan pages,
orphan sources, stale articles, missing backlinks, sparse articles, and weak
graph connectivity. The full mode also runs an LLM contradiction check.

Usage:
    uv run python lint.py                    # all checks
    uv run python lint.py --structural-only  # skip LLM checks (faster, cheaper)
"""

from __future__ import annotations

import argparse
import asyncio
import re
from pathlib import Path

from codex_exec import run_codex_prompt
from config import KNOWLEDGE_DIR, LLM_LOCK_FILE, REPORTS_DIR, now_iso, today_iso
from locking import file_lock
from runtime_config import get_codex_model, get_task_runtime
from utils import (
    count_inbound_links,
    daily_source_exists,
    extract_wikilinks,
    file_hash,
    find_missing_index_targets,
    find_unindexed_articles,
    get_article_word_count,
    list_raw_files,
    list_wiki_articles,
    load_state,
    read_all_wiki_content,
    update_state,
    wiki_article_exists,
)

ROOT_DIR = Path(__file__).resolve().parent.parent


def check_broken_links() -> list[dict]:
    """Check for [[wikilinks]] that point to non-existent articles."""
    issues = []
    for article in list_wiki_articles():
        content = article.read_text(encoding="utf-8")
        rel = article.relative_to(KNOWLEDGE_DIR)
        for link in extract_wikilinks(content):
            if link.startswith("daily/"):
                if not daily_source_exists(link):
                    issues.append({
                        "severity": "error",
                        "check": "broken_link",
                        "file": str(rel),
                        "detail": f"Broken source link: [[{link}]] - daily log does not exist",
                    })
                continue
            if not wiki_article_exists(link):
                issues.append({
                    "severity": "error",
                    "check": "broken_link",
                    "file": str(rel),
                    "detail": f"Broken link: [[{link}]] - target does not exist",
                })
    return issues


def check_index_consistency() -> list[dict]:
    """Check that every article on disk is reachable from knowledge/index.md."""
    issues = []

    for link in find_unindexed_articles():
        issues.append({
            "severity": "error",
            "check": "index_consistency",
            "subcheck": "unindexed_article",
            "file": f"{link}.md",
            "target": link,
            "detail": f"Article exists on disk but is missing from knowledge/index.md: [[{link}]]",
            "auto_fixable": True,
        })

    for link in find_missing_index_targets():
        issues.append({
            "severity": "error",
            "check": "index_consistency",
            "subcheck": "missing_target",
            "file": "index.md",
            "target": link,
            "detail": f"knowledge/index.md references missing article: [[{link}]]",
        })

    return issues


def check_orphan_pages() -> list[dict]:
    """Check for articles with zero inbound links."""
    issues = []
    for article in list_wiki_articles():
        rel = article.relative_to(KNOWLEDGE_DIR)
        link_target = str(rel).replace(".md", "").replace("\\", "/")
        inbound = count_inbound_links(link_target)
        if inbound == 0:
            issues.append({
                "severity": "warning",
                "check": "orphan_page",
                "file": str(rel),
                "detail": f"Orphan page: no other articles link to [[{link_target}]]",
            })
    return issues


def check_orphan_sources() -> list[dict]:
    """Check for daily logs that haven't been compiled yet."""
    state = load_state()
    ingested = state.get("ingested", {})
    issues = []
    for log_path in list_raw_files():
        if log_path.name not in ingested:
            issues.append({
                "severity": "warning",
                "check": "orphan_source",
                "file": f"daily/{log_path.name}",
                "detail": f"Uncompiled daily log: {log_path.name} has not been ingested",
            })
    return issues


def check_stale_articles() -> list[dict]:
    """Check if source daily logs have changed since compilation."""
    state = load_state()
    ingested = state.get("ingested", {})
    issues = []
    for log_path in list_raw_files():
        rel = log_path.name
        if rel in ingested:
            stored_hash = ingested[rel].get("hash", "")
            current_hash = file_hash(log_path)
            if stored_hash != current_hash:
                issues.append({
                    "severity": "warning",
                    "check": "stale_article",
                    "file": f"daily/{rel}",
                    "detail": f"Stale: {rel} has changed since last compilation",
                })
    return issues


def check_missing_backlinks() -> list[dict]:
    """Check for asymmetric links: A links to B but B doesn't link to A."""
    issues = []
    for article in list_wiki_articles():
        content = article.read_text(encoding="utf-8")
        rel = article.relative_to(KNOWLEDGE_DIR)
        source_link = str(rel).replace(".md", "").replace("\\", "/")

        for link in extract_wikilinks(content):
            if link.startswith("daily/"):
                continue
            target_path = KNOWLEDGE_DIR / f"{link}.md"
            if target_path.exists():
                target_content = target_path.read_text(encoding="utf-8")
                if source_link not in extract_wikilinks(target_content):
                    issues.append({
                        "severity": "suggestion",
                        "check": "missing_backlink",
                        "file": str(rel),
                        "source": source_link,
                        "target": link,
                        "detail": f"[[{source_link}]] links to [[{link}]] but not vice versa",
                        "auto_fixable": True,
                    })
    return issues


MAX_INDEX_SUMMARY_CHARS = 200
MAX_INDEX_SOURCES = 3

_INDEX_ROW_RE = re.compile(
    r"^\|\s*\[\[([^\]]+)\]\]\s*\|\s*(.*?)\s*\|\s*(.*?)\s*\|\s*([0-9]{4}-[0-9]{2}-[0-9]{2})\s*\|\s*$"
)


def check_index_hygiene() -> list[dict]:
    """Flag index rows that bloat the index: run-on summaries and source sprawl.

    The session-start hook injects a tiered slice of the index into every
    conversation; row size directly eats that budget. Summaries should be
    one line of essence, and long source lists should collapse to
    'first, latest +N more'.
    """
    index_path = KNOWLEDGE_DIR / "index.md"
    if not index_path.exists():
        return []

    issues = []
    for line in index_path.read_text(encoding="utf-8").splitlines():
        m = _INDEX_ROW_RE.match(line.strip())
        if not m:
            continue
        link, summary, sources, _updated = m.groups()

        if len(summary) > MAX_INDEX_SUMMARY_CHARS:
            issues.append({
                "severity": "suggestion",
                "check": "index_hygiene",
                "subcheck": "long_summary",
                "file": "index.md",
                "target": link,
                "detail": (
                    f"[[{link}]] index summary is {len(summary)} chars "
                    f"(max {MAX_INDEX_SUMMARY_CHARS}). Rewrite as one line of essence; "
                    "history belongs in the article body."
                ),
            })

        source_count = sources.count(",") + 1 if sources.strip() else 0
        if source_count > MAX_INDEX_SOURCES:
            issues.append({
                "severity": "suggestion",
                "check": "index_hygiene",
                "subcheck": "source_sprawl",
                "file": "index.md",
                "target": link,
                "source_cell": sources,
                "detail": (
                    f"[[{link}]] lists {source_count} sources in the index "
                    f"(max {MAX_INDEX_SOURCES}). Collapse to 'first, latest +N more' "
                    "(auto-fixable); the full list lives in the article frontmatter."
                ),
                "auto_fixable": True,
            })

    return issues


def fix_index_source_sprawl(issues: list[dict]) -> int:
    """Collapse sprawling source cells to 'first, latest +N more'. Returns rows fixed."""
    index_path = KNOWLEDGE_DIR / "index.md"
    if not index_path.exists():
        return 0

    cells_by_target: dict[str, str] = {
        i["target"]: i["source_cell"]
        for i in issues
        if i.get("check") == "index_hygiene"
        and i.get("subcheck") == "source_sprawl"
        and i.get("target")
        and i.get("source_cell")
    }
    if not cells_by_target:
        return 0

    text = index_path.read_text(encoding="utf-8")
    fixed = 0
    for target, cell in cells_by_target.items():
        parts = [p.strip() for p in cell.split(",") if p.strip()]
        if len(parts) <= MAX_INDEX_SOURCES:
            continue
        collapsed = f"{parts[0]}, {parts[-1]} +{len(parts) - 2} more"
        new_text = text.replace(f"| {cell} |", f"| {collapsed} |", 1)
        if new_text != text:
            text = new_text
            fixed += 1
    if fixed:
        index_path.write_text(text, encoding="utf-8")
    return fixed


def check_sparse_articles() -> list[dict]:
    """Check for articles with fewer than 200 words."""
    issues = []
    for article in list_wiki_articles():
        word_count = get_article_word_count(article)
        if word_count < 200:
            rel = article.relative_to(KNOWLEDGE_DIR)
            issues.append({
                "severity": "suggestion",
                "check": "sparse_article",
                "file": str(rel),
                "detail": f"Sparse article: {word_count} words (minimum recommended: 200)",
            })
    return issues


def check_weak_connectivity(
    max_issues: int = 25,
    min_inbound_links: int = 2,
    min_total_links: int = 4,
) -> list[dict]:
    """Identify articles that are reachable but weakly connected to the graph."""
    articles = list_wiki_articles()
    article_links = {
        str(article.relative_to(KNOWLEDGE_DIR)).replace(".md", "").replace("\\", "/")
        for article in articles
    }
    outbound: dict[str, set[str]] = {link: set() for link in article_links}
    inbound: dict[str, set[str]] = {link: set() for link in article_links}

    for article in articles:
        source = str(article.relative_to(KNOWLEDGE_DIR)).replace(".md", "").replace("\\", "/")
        for target in extract_wikilinks(article.read_text(encoding="utf-8")):
            if target in article_links:
                outbound[source].add(target)
                inbound[target].add(source)

    candidates = []
    for link in sorted(article_links):
        inbound_count = len(inbound[link])
        outbound_count = len(outbound[link])
        total_count = inbound_count + outbound_count
        if inbound_count < min_inbound_links or total_count < min_total_links:
            candidates.append((inbound_count, total_count, outbound_count, link))

    candidates.sort(key=lambda item: (item[0], item[1], item[3]))

    issues = []
    for inbound_count, total_count, outbound_count, link in candidates[:max_issues]:
        issues.append({
            "severity": "suggestion",
            "check": "weak_connectivity",
            "file": f"{link}.md",
            "detail": (
                f"Weak graph connectivity: {inbound_count} inbound, "
                f"{outbound_count} outbound. Add 1-3 semantic Related Concepts links "
                "from/to relevant hub articles if the relationship is real."
            ),
        })
    return issues


async def check_contradictions() -> list[dict]:
    """Use LLM to detect contradictions across articles."""
    wiki_content = read_all_wiki_content()

    prompt = f"""Review this knowledge base for contradictions, inconsistencies, or
conflicting claims across articles.

## Knowledge Base

{wiki_content}

## Instructions

Look for:
- Direct contradictions (article A says X, article B says not-X)
- Inconsistent recommendations (different articles recommend conflicting approaches)
- Outdated information that conflicts with newer entries

For each issue found, output EXACTLY one line in this format:
CONTRADICTION: [file1] vs [file2] - description of the conflict
INCONSISTENCY: [file] - description of the inconsistency

If no issues found, output exactly: NO_ISSUES

Do NOT output anything else - no preamble, no explanation, just the formatted lines."""

    response = ""
    runtime = get_task_runtime("lint")
    try:
        # Serialize against flush/compile LLM calls — concurrent bundled-CLI
        # instances crash each other with exit 1.
        with file_lock(LLM_LOCK_FILE):
            if runtime == "codex":
                response = await asyncio.to_thread(
                    run_codex_prompt,
                    prompt,
                    cwd=ROOT_DIR,
                    allow_edits=False,
                    model=get_codex_model(),
                )
            else:
                from claude_agent_sdk import (
                    AssistantMessage,
                    ClaudeAgentOptions,
                    TextBlock,
                    query,
                )

                async for message in query(
                    prompt=prompt,
                    options=ClaudeAgentOptions(
                        cwd=str(ROOT_DIR),
                        allowed_tools=[],
                        max_turns=2,
                    ),
                ):
                    if isinstance(message, AssistantMessage):
                        for block in message.content:
                            if isinstance(block, TextBlock):
                                response += block.text
    except Exception as e:
        return [{"severity": "error", "check": "contradiction", "file": "(system)", "detail": f"LLM check failed: {e}"}]

    issues = []
    if "NO_ISSUES" not in response:
        for line in response.strip().split("\n"):
            line = line.strip()
            if line.startswith("CONTRADICTION:") or line.startswith("INCONSISTENCY:"):
                issues.append({
                    "severity": "warning",
                    "check": "contradiction",
                    "file": "(cross-article)",
                    "detail": line,
                })

    return issues


# =====================================================================
# Auto-fixers
# =====================================================================
#
# These functions are imported by compile.py so a post-compile lint can
# auto-recover from drift instead of bailing with "please fix manually".


_FRONTMATTER_FIELD = re.compile(r"^([a-zA-Z_]+)\s*:\s*(.*)$")


def _parse_frontmatter(text: str) -> dict:
    """Tiny YAML-frontmatter parser. Returns a dict of scalar fields and
    flat lists. Good enough for our article frontmatter; not a real YAML
    implementation."""
    if not text.startswith("---"):
        return {}
    end = text.find("\n---", 3)
    if end == -1:
        return {}
    body = text[3:end].strip("\n")
    out: dict = {}
    current_list_key: str | None = None
    for raw in body.splitlines():
        line = raw.rstrip()
        if line.startswith("  - ") and current_list_key:
            out.setdefault(current_list_key, []).append(line[4:].strip().strip('"').strip("'"))
            continue
        m = _FRONTMATTER_FIELD.match(line)
        if not m:
            current_list_key = None
            continue
        key, value = m.group(1), m.group(2).strip()
        if value == "":
            current_list_key = key
            continue
        current_list_key = None
        out[key] = value.strip('"').strip("'")
    return out


def _insert_backlink(text: str, source_wikilink: str) -> str:
    """Insert `- [[source]]` into '## Related Concepts'. Create the
    section before '## Sources' if missing. Idempotent: skips if the
    backlink already exists anywhere in the text."""
    if f"[[{source_wikilink}]]" in text:
        return text
    new_line = f"- [[{source_wikilink}]]"
    lines = text.splitlines(keepends=False)

    related_idx = None
    sources_idx = None
    for i, line in enumerate(lines):
        if line.strip() == "## Related Concepts":
            related_idx = i
        elif line.strip() == "## Sources":
            sources_idx = i

    if related_idx is not None:
        end = len(lines)
        for j in range(related_idx + 1, len(lines)):
            if lines[j].startswith("## "):
                end = j
                break
        insert_at = end
        while insert_at > related_idx + 1 and lines[insert_at - 1].strip() == "":
            insert_at -= 1
        lines.insert(insert_at, new_line)
    else:
        block = ["", "## Related Concepts", "", new_line, ""]
        if sources_idx is not None:
            for k, b in enumerate(block):
                lines.insert(sources_idx + k, b)
        else:
            if lines and lines[-1].strip() != "":
                lines.append("")
            lines.extend(["## Related Concepts", "", new_line])

    return "\n".join(lines) + ("\n" if text.endswith("\n") else "")


def fix_missing_backlinks(issues: list[dict]) -> int:
    """Apply auto-fix for symmetric backlinks. Returns count of links added."""
    by_target: dict[str, list[str]] = {}
    for issue in issues:
        if issue.get("check") != "missing_backlink" or not issue.get("auto_fixable"):
            continue
        target = issue.get("target")
        source = issue.get("source")
        if not target or not source:
            continue
        by_target.setdefault(target, []).append(source)

    written = 0
    for target, sources in by_target.items():
        target_path = KNOWLEDGE_DIR / f"{target}.md"
        if not target_path.exists():
            continue
        content = target_path.read_text(encoding="utf-8")
        modified = content
        for source in sources:
            new_content = _insert_backlink(modified, source)
            if new_content != modified:
                written += 1
                modified = new_content
        if modified != content:
            target_path.write_text(modified, encoding="utf-8")
    return written


def fix_index_consistency(issues: list[dict]) -> int:
    """Append index rows for unindexed articles using their frontmatter.

    Stub-row format: `| [[slug]] | <title> | <source> | <updated> |`. The
    LLM may later refine the summary; this just gets the article visible
    in the index so it stops being a structural error.

    Returns count of rows appended.
    """
    targets: list[str] = []
    for issue in issues:
        if issue.get("check") != "index_consistency":
            continue
        if issue.get("subcheck") != "unindexed_article":
            continue
        target = issue.get("target")
        if target and target not in targets:
            targets.append(target)

    if not targets:
        return 0

    index_path = KNOWLEDGE_DIR / "index.md"
    if not index_path.exists():
        return 0
    index_text = index_path.read_text(encoding="utf-8")

    new_rows: list[str] = []
    for target in targets:
        article_path = KNOWLEDGE_DIR / f"{target}.md"
        if not article_path.exists():
            continue
        article_text = article_path.read_text(encoding="utf-8")
        fm = _parse_frontmatter(article_text)
        title = fm.get("title") or target.split("/")[-1].replace("-", " ").title()
        sources = fm.get("sources") or []
        source_str = sources[0] if isinstance(sources, list) and sources else "(unknown)"
        if isinstance(sources, list) and len(sources) > 1:
            source_str = ", ".join(sources)
        updated = fm.get("updated") or fm.get("created") or today_iso()
        row = f"| [[{target}]] | {title} (auto-stub: refine summary on next compile) | {source_str} | {updated} |"
        new_rows.append(row)

    if not new_rows:
        return 0

    if not index_text.endswith("\n"):
        index_text += "\n"
    index_text += "\n".join(new_rows) + "\n"
    index_path.write_text(index_text, encoding="utf-8")
    return len(new_rows)


def apply_fixes(all_issues: list[dict]) -> dict:
    """Apply all auto-fixers. Returns counts per fixer."""
    return {
        "backlinks_added": fix_missing_backlinks(all_issues),
        "index_rows_added": fix_index_consistency(all_issues),
        "source_cells_collapsed": fix_index_source_sprawl(all_issues),
    }


# =====================================================================
# Report
# =====================================================================


def generate_report(all_issues: list[dict]) -> str:
    """Generate a markdown lint report."""
    errors = [i for i in all_issues if i["severity"] == "error"]
    warnings = [i for i in all_issues if i["severity"] == "warning"]
    suggestions = [i for i in all_issues if i["severity"] == "suggestion"]

    lines = [
        f"# Lint Report - {today_iso()}",
        "",
        f"**Total issues:** {len(all_issues)}",
        f"- Errors: {len(errors)}",
        f"- Warnings: {len(warnings)}",
        f"- Suggestions: {len(suggestions)}",
        "",
    ]

    for severity, issues, marker in [
        ("Errors", errors, "x"),
        ("Warnings", warnings, "!"),
        ("Suggestions", suggestions, "?"),
    ]:
        if issues:
            lines.append(f"## {severity}")
            lines.append("")
            for issue in issues:
                fixable = " (auto-fixable)" if issue.get("auto_fixable") else ""
                lines.append(f"- **[{marker}]** `{issue['file']}` - {issue['detail']}{fixable}")
            lines.append("")

    if not all_issues:
        lines.append("All checks passed. Knowledge base is healthy.")
        lines.append("")

    return "\n".join(lines)


def _run_structural_checks() -> list[dict]:
    """Run all structural checks and return the combined issue list."""
    all_issues: list[dict] = []
    checks = [
        ("Broken links", check_broken_links),
        ("Index consistency", check_index_consistency),
        ("Index hygiene", check_index_hygiene),
        ("Orphan pages", check_orphan_pages),
        ("Orphan sources", check_orphan_sources),
        ("Stale articles", check_stale_articles),
        ("Missing backlinks", check_missing_backlinks),
        ("Sparse articles", check_sparse_articles),
        ("Weak connectivity", check_weak_connectivity),
    ]
    for name, check_fn in checks:
        print(f"  Checking: {name}...")
        issues = check_fn()
        all_issues.extend(issues)
        print(f"    Found {len(issues)} issue(s)")
    return all_issues


def main():
    parser = argparse.ArgumentParser(description="Lint the knowledge base")
    parser.add_argument(
        "--structural-only",
        action="store_true",
        help="Skip LLM-based checks (contradictions) - faster and free",
    )
    parser.add_argument(
        "--fix",
        action="store_true",
        help=(
            "Apply auto-fixes for known mechanical issues: symmetric "
            "backlinks and unindexed-article stub rows. Re-runs structural "
            "checks after fixing to verify."
        ),
    )
    args = parser.parse_args()

    print("Running knowledge base lint checks...")
    all_issues = _run_structural_checks()

    # LLM check (costs money) — skipped under --fix to keep the fix loop fast and free
    if not args.structural_only and not args.fix:
        print("  Checking: Contradictions (LLM)...")
        issues = asyncio.run(check_contradictions())
        all_issues.extend(issues)
        print(f"    Found {len(issues)} issue(s)")
    elif args.structural_only:
        print("  Skipping: Contradictions (--structural-only)")
    else:
        print("  Skipping: Contradictions (--fix implies structural-only)")

    # Auto-fix pass
    if args.fix:
        print("\nApplying auto-fixes...")
        counts = apply_fixes(all_issues)
        for key, value in counts.items():
            print(f"  {key.replace('_', ' ').capitalize()}: {value}")
        if any(counts.values()):
            print("\nRe-running structural checks after fix...")
            all_issues = _run_structural_checks()
        else:
            print("  Nothing to fix.")

    # Generate and save report
    report = generate_report(all_issues)
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    report_path = REPORTS_DIR / f"lint-{today_iso()}.md"
    report_path.write_text(report, encoding="utf-8")
    print(f"\nReport saved to: {report_path}")

    # Update state
    def mutate(state: dict) -> None:
        state["last_lint"] = now_iso()

    update_state(mutate)

    # Summary
    errors = sum(1 for i in all_issues if i["severity"] == "error")
    warnings = sum(1 for i in all_issues if i["severity"] == "warning")
    suggestions = sum(1 for i in all_issues if i["severity"] == "suggestion")
    print(f"\nResults: {errors} errors, {warnings} warnings, {suggestions} suggestions")

    if errors > 0:
        print("\nErrors found - knowledge base needs attention!")
        return 1
    return 0


if __name__ == "__main__":
    exit(main())
