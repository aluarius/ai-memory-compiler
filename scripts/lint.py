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
import sqlite3
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

import kb_db
from article_schema import source_path
from codex_exec import run_codex_prompt
from config import KNOWLEDGE_DIR, LLM_LOCK_FILE, REPORTS_DIR, now_iso, today_iso
from locking import file_lock
from memory_export import export_memory
from memory_store import MemoryStore
from migration_gate import guard_legacy_writer
from model_runtime import call_readonly_model
from runtime_config import get_claude_model, get_codex_model, get_task_runtime
from utils import (
    INDEX_ROW_RE,
    daily_source_exists,
    extract_wikilinks,
    file_hash,
    find_missing_index_targets,
    find_unindexed_articles,
    list_raw_files,
    list_wiki_articles,
    load_state,
    update_state,
)

ROOT_DIR = Path(__file__).resolve().parent.parent
MAX_SEMANTIC_LINT_CANDIDATES = 12
MAX_SEMANTIC_LINT_PROMPT_CHARS = 750_000


@dataclass(frozen=True)
class GraphSnapshot:
    articles: dict[str, dict]
    links: dict[str, set[str]]
    inbound: dict[str, set[str]]
    sources: set[str]
    canonical: dict | None = None


def load_graph(snapshot: dict | None = None) -> GraphSnapshot:
    """Parse every body once and build adjacency in O(articles + links)."""
    if snapshot is None and MemoryStore.is_initialized(KNOWLEDGE_DIR.parent):
        snapshot = MemoryStore(KNOWLEDGE_DIR.parent, readonly=True).snapshot()
    if snapshot is not None:
        articles = {article["path"]: article for article in snapshot["articles"]}
    else:
        articles = {
            path.relative_to(KNOWLEDGE_DIR).as_posix().removesuffix(".md"): {
                "body": path.read_text(encoding="utf-8"),
            }
            for path in list_wiki_articles()
        }
    links = {
        path: {link.split("#", 1)[0].removesuffix(".md") for link in extract_wikilinks(article["body"]) if link.split("#", 1)[0]}
        for path, article in articles.items()
    }
    inbound: dict[str, set[str]] = {path: set() for path in articles}
    for path, targets in links.items():
        for target in targets:
            if target in inbound and target != path:
                inbound[target].add(path)
    return GraphSnapshot(
        articles=articles, links=links, inbound=inbound,
        sources={source["path"] for source in snapshot["sources"]} if snapshot is not None else set(),
        canonical=snapshot,
    )


def check_broken_links(graph: GraphSnapshot | None = None) -> list[dict]:
    """Check for [[wikilinks]] that point to non-existent articles."""
    issues = []
    graph = graph or load_graph()
    for article, links in graph.links.items():
        rel = f"{article}.md"
        for link in sorted(links):
            if link.startswith("daily/"):
                try:
                    exists = source_path(link) in graph.sources if graph.canonical is not None else daily_source_exists(link)
                except ValueError:
                    exists = False
                if not exists:
                    issues.append({
                        "severity": "error",
                        "check": "broken_link",
                        "file": str(rel),
                        "detail": f"Broken source link: [[{link}]] - daily log does not exist",
                    })
                continue
            if link not in graph.articles:
                issues.append({
                    "severity": "error",
                    "check": "broken_link",
                    "file": str(rel),
                    "detail": f"Broken link: [[{link}]] - target does not exist",
                })
    return issues


def check_index_consistency(graph: GraphSnapshot | None = None) -> list[dict]:
    """Check that every article on disk is reachable from knowledge/index.md."""
    issues = []
    if (graph is not None and graph.canonical is not None) or (
        graph is None and MemoryStore.is_initialized(KNOWLEDGE_DIR.parent)
    ):
        # The index is generated from the article table; export drift is operational.
        return issues

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


def check_orphan_pages(graph: GraphSnapshot | None = None) -> list[dict]:
    """Check for articles with zero inbound links."""
    issues = []
    graph = graph or load_graph()
    for link_target in sorted(graph.articles):
        rel = f"{link_target}.md"
        if not graph.inbound[link_target]:
            issues.append({
                "severity": "warning",
                "check": "orphan_page",
                "file": str(rel),
                "detail": f"Orphan page: no other articles link to [[{link_target}]]",
            })
    return issues


def check_orphan_sources(graph: GraphSnapshot | None = None) -> list[dict]:
    """Check for daily logs that haven't been compiled yet."""
    graph = graph or load_graph()
    if graph.canonical is not None:
        uncompiled, _ = source_backlog(graph.canonical)
        return [{"severity": "warning", "check": "orphan_source", "file": f"daily/{name}",
                 "detail": f"Uncompiled daily log: {name} has not been ingested"} for name in uncompiled]
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


def check_stale_articles(graph: GraphSnapshot | None = None) -> list[dict]:
    """Check if source daily logs have changed since compilation."""
    graph = graph or load_graph()
    if graph.canonical is not None:
        _, stale = source_backlog(graph.canonical)
        return [{"severity": "warning", "check": "stale_article", "file": f"daily/{name}",
                 "detail": f"Stale: {name} has changed since last compilation"} for name in stale]
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


def source_backlog(snapshot: dict) -> tuple[list[str], list[str]]:
    """Compare canonical source hashes with committed ingestion checkpoints."""
    ingested = snapshot["pipeline"].get("ingested", {})
    uncompiled: list[str] = []
    stale: list[str] = []
    for source in snapshot["sources"]:
        name = source["path"].removeprefix("daily/")
        metadata = ingested.get(name)
        if metadata is None:
            uncompiled.append(name)
        else:
            digest = metadata.get("hash")
            if not isinstance(digest, str) or len(digest) not in (16, 64) or not source["content_hash"].startswith(digest):
                stale.append(name)
    return uncompiled, stale


def check_missing_backlinks(graph: GraphSnapshot | None = None) -> list[dict]:
    """Check for asymmetric links: A links to B but B doesn't link to A."""
    issues = []
    graph = graph or load_graph()
    for source_link, links in graph.links.items():
        rel = f"{source_link}.md"
        for link in sorted(links):
            if link in graph.articles:
                if source_link not in graph.links[link]:
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

# A legitimate sources cell is a comma list of daily-log refs (optionally
# collapsed with '+N more'). Anything else means the row's cells mis-parsed —
# e.g. a raw '|' inside the summary shifted the columns — and auto-collapsing
# it would destroy summary content.
_SOURCE_PART_RE = re.compile(r"^(?:\[\[)?(?:daily/)?[\w./-]+(?:\]\])?(?:\s\+\d+ more)?$")


def _looks_like_sources_cell(cell: str) -> bool:
    parts = [p.strip() for p in cell.split(",") if p.strip()]
    return bool(parts) and all(_SOURCE_PART_RE.match(p) for p in parts)


def check_index_hygiene(graph: GraphSnapshot | None = None) -> list[dict]:
    """Flag index rows that bloat the index: run-on summaries and source sprawl.

    The session-start hook injects a tiered slice of the index into every
    conversation; row size directly eats that budget. Summaries should be
    one line of essence, and long source lists should collapse to
    'first, latest +N more'.
    """
    if graph is None and MemoryStore.is_initialized(KNOWLEDGE_DIR.parent):
        graph = load_graph()
    if graph is not None and graph.canonical is not None:
        return [{
            "severity": "suggestion", "check": "index_hygiene", "subcheck": "long_summary",
            "file": f"{path}.md", "target": path,
            "detail": f"Index summary is {len(article['summary'])} chars (max {MAX_INDEX_SUMMARY_CHARS}).",
        } for path, article in graph.articles.items() if len(article["summary"]) > MAX_INDEX_SUMMARY_CHARS]
    index_path = KNOWLEDGE_DIR / "index.md"
    if not index_path.exists():
        return []

    issues = []
    for line in index_path.read_text(encoding="utf-8").splitlines():
        m = INDEX_ROW_RE.match(line.strip())
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
            fixable = _looks_like_sources_cell(sources)
            issue = {
                "severity": "suggestion",
                "check": "index_hygiene",
                "subcheck": "source_sprawl",
                "file": "index.md",
                "target": link,
                "detail": (
                    f"[[{link}]] lists {source_count} sources in the index "
                    f"(max {MAX_INDEX_SOURCES}). Collapse to 'first, latest +N more'"
                    + (
                        " (auto-fixable); the full list lives in the article frontmatter."
                        if fixable
                        else "; cell does not parse as a source list (raw '|' in the "
                        "summary?) — fix the row by hand."
                    )
                ),
            }
            if fixable:
                issue["source_cell"] = sources
                issue["auto_fixable"] = True
            issues.append(issue)

    return issues


def fix_index_source_sprawl(issues: list[dict]) -> int:
    """Collapse sprawling source cells to 'first, latest +N more'. Returns rows fixed."""
    if MemoryStore.is_initialized(KNOWLEDGE_DIR.parent):
        return 0  # Canonical export already limits the sources cell.
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
    for cell in cells_by_target.values():
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


def check_sparse_articles(graph: GraphSnapshot | None = None) -> list[dict]:
    """Check for articles with fewer than 200 words."""
    issues = []
    graph = graph or load_graph()
    for path, article in graph.articles.items():
        body = re.sub(r"\A---\r?\n.*?\r?\n---(?:\r?\n|\Z)", "", article["body"], count=1, flags=re.DOTALL)
        word_count = len(body.split())
        if word_count < 200:
            rel = f"{path}.md"
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
    graph: GraphSnapshot | None = None,
) -> list[dict]:
    """Identify articles that are reachable but weakly connected to the graph."""
    graph = graph or load_graph()
    article_links = graph.articles.keys()
    outbound = {path: (links & article_links) - {path} for path, links in graph.links.items()}
    inbound = graph.inbound

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


def _semantic_candidate_blocks() -> list[str] | None:
    """Return bounded full-text pairs most likely to expose semantic conflicts.

    Mutual FTS neighbours are the strongest local signal that two articles make
    comparable claims. The SQLite index is derived data; ``None`` means it is
    unavailable and must be rebuilt rather than silently weakening this check.
    """
    pairs = kb_db.find_similar_pairs(limit=MAX_SEMANTIC_LINT_CANDIDATES)
    if pairs is None:
        return None

    blocks: list[str] = []
    size = 0
    store = MemoryStore(KNOWLEDGE_DIR.parent, readonly=True) if MemoryStore.is_initialized(KNOWLEDGE_DIR.parent) else None
    canonical = {article["path"]: article for article in store.snapshot()["articles"]} if store else None
    for pair in pairs:
        a = pair.get("a")
        b = pair.get("b")
        if not isinstance(a, str) or not isinstance(b, str):
            continue
        try:
            if canonical is not None:
                a_article, b_article = canonical.get(a), canonical.get(b)
                if a_article is None or b_article is None:
                    raise ValueError("Semantic candidate missing from canonical memory")
                a_body, b_body = a_article["body"], b_article["body"]
            else:
                a_body = (KNOWLEDGE_DIR / f"{a}.md").read_text(encoding="utf-8")
                b_body = (KNOWLEDGE_DIR / f"{b}.md").read_text(encoding="utf-8")
        except OSError:
            continue

        block = (
            f"### {a} vs {b} (similarity score {pair.get('score', 'unknown')})\n\n"
            f"#### {a}\n\n{a_body}\n\n#### {b}\n\n{b_body}"
        )
        if size + len(block) > MAX_SEMANTIC_LINT_PROMPT_CHARS:
            continue
        blocks.append(block)
        size += len(block)
    return blocks


def build_contradiction_prompt(candidate_blocks: list[str]) -> str:
    """Build a bounded semantic-lint prompt from verified full-text candidates."""
    pairs = "\n\n---\n\n".join(candidate_blocks)
    return f"""Review this knowledge base for real contradictions, inconsistencies, or
conflicting claims across articles. This is a read-only review: do not edit any files.

## Candidate Article Pairs

{pairs}

The pairs are mutual full-text-search neighbours, selected because they cover
similar subjects. Their full source text is included above. Do not infer a conflict
from titles, summaries, or omitted articles.

Look for:
- Direct contradictions (article A says X, article B says not-X)
- Inconsistent recommendations for the same scope and conditions
- Outdated information that conflicts with a newer entry on the same subject

Do not flag differences in date, project, environment, or stated assumptions when
they can both be true. Prefer no finding when the evidence is ambiguous.

For each verified issue found, output EXACTLY one line in this format:
CONTRADICTION: [file1] vs [file2] - description of the conflict
INCONSISTENCY: [file] - description of the inconsistency

If no verified issues are found, output exactly: NO_ISSUES

Do NOT output anything else - no preamble, no explanation, just the formatted lines."""


async def check_contradictions() -> list[dict]:
    """Use an LLM to verify bounded full-text candidates for contradictions."""
    try:
        candidate_blocks = _semantic_candidate_blocks()
    except (sqlite3.Error, ValueError, OSError) as exc:
        return [{"severity": "error", "check": "contradiction", "file": "(system)", "detail": f"Semantic candidates unavailable: {exc}"}]
    if candidate_blocks is None:
        return [{
            "severity": "error",
            "check": "contradiction",
            "file": "(system)",
            "detail": "Semantic check unavailable: rebuild scripts/kb-index.sqlite first.",
        }]
    if not candidate_blocks:
        return []
    prompt = build_contradiction_prompt(candidate_blocks)

    response = ""
    runtime = get_task_runtime("lint")
    try:
        # Serialize against flush/compile LLM calls — concurrent bundled-CLI
        # instances crash each other with exit 1.
        with file_lock(LLM_LOCK_FILE):
            if MemoryStore.is_initialized(KNOWLEDGE_DIR.parent):
                with tempfile.TemporaryDirectory(prefix="memory-lint-") as temporary:
                    result = await call_readonly_model(prompt, cwd=Path(temporary), task="lint")
                    response = result.text
            elif runtime == "codex":
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
                        model=get_claude_model(),
                        tools=[],
                        allowed_tools=[],
                        setting_sources=[],
                        env={"CLAUDE_INVOKED_BY": "memory_lint", "MEMORY_COMPILER_INTERNAL": "1"},
                        max_turns=2,
                    ),
                ):
                    if isinstance(message, AssistantMessage):
                        for block in message.content:
                            if isinstance(block, TextBlock):
                                response += block.text
    except Exception as e:  # noqa: BLE001 - the external LLM boundary must never abort lint.
        return [{"severity": "error", "check": "contradiction", "file": "(system)", "detail": f"LLM check failed: {e}"}]

    if response.strip() == "NO_ISSUES":
        return []
    lines = [line.strip() for line in response.splitlines() if line.strip()]
    if not lines or any(not line.startswith(("CONTRADICTION:", "INCONSISTENCY:")) for line in lines):
        return [{"severity": "error", "check": "contradiction", "file": "(system)", "detail": "Semantic check returned an invalid or empty response."}]
    return [{"severity": "warning", "check": "contradiction", "file": "(cross-article)", "detail": line} for line in lines]


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
    if MemoryStore.is_initialized(KNOWLEDGE_DIR.parent):
        return _apply_canonical_fixes(issues)["backlinks_added"]
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
    if MemoryStore.is_initialized(KNOWLEDGE_DIR.parent):
        return 0
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
    if MemoryStore.is_initialized(KNOWLEDGE_DIR.parent):
        return _apply_canonical_fixes(all_issues)
    return {
        "backlinks_added": fix_missing_backlinks(all_issues),
        "index_rows_added": fix_index_consistency(all_issues),
        "source_cells_collapsed": fix_index_source_sprawl(all_issues),
    }


def _apply_canonical_fixes(issues: list[dict]) -> dict[str, int]:
    """Commit validated mechanical edits together, then refresh the projection."""
    store = MemoryStore(KNOWLEDGE_DIR.parent)
    snapshot = store.snapshot()
    graph = load_graph(snapshot)
    articles = graph.articles
    changed: dict[str, dict] = {}
    links_added = 0
    for issue in issues:
        if issue.get("check") != "missing_backlink" or not issue.get("auto_fixable"):
            continue
        source, target = issue.get("source"), issue.get("target")
        if source not in articles or target not in articles:
            continue
        # Recheck the current source after a concurrent compile, rather than
        # applying relationships that disappeared since the lint snapshot.
        if target not in graph.links[source]:
            continue
        article = changed.get(target, articles[target])
        body = _insert_backlink(article["body"], source)
        if body != article["body"]:
            changed[target] = {**article, "body": body}
            links_added += 1
    if changed:
        store.commit_articles(
            list(changed.values()), expected_generation=snapshot["generation"],
            build_entry=f"\n## [{now_iso()}] lint | Mechanical repairs\n- Backlinks added: {links_added}\n",
        )
    export_memory(store)
    return {"backlinks_added": links_added, "index_rows_added": 0, "source_cells_collapsed": 0}


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


def structural_checks(snapshot: dict | None = None, *, verbose: bool = False) -> list[dict]:
    """Run a pass against one graph; SQLite errors never select legacy data."""
    try:
        graph = load_graph(snapshot)
    except (sqlite3.Error, ValueError, OSError) as exc:
        return [{"severity": "error", "check": "database", "file": "(system)", "detail": str(exc)}]
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
        if verbose:
            print(f"  Checking: {name}...")
        issues = check_fn(graph=graph)
        all_issues.extend(issues)
        if verbose:
            print(f"    Found {len(issues)} issue(s)")
    return all_issues


def _run_structural_checks() -> list[dict]:
    return structural_checks(verbose=True)


@guard_legacy_writer(lambda: KNOWLEDGE_DIR.parent)
def main() -> int:
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
    if any(issue["check"] == "database" for issue in all_issues):
        print(generate_report(all_issues))
        return 1

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

    if MemoryStore.is_initialized(KNOWLEDGE_DIR.parent):
        MemoryStore(KNOWLEDGE_DIR.parent).update_state("pipeline", mutate)
    else:
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
    sys.exit(main())
