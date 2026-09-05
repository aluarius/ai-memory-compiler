"""
MCP server for the personal knowledge base.

Exposes tools to search and read knowledge base articles from any
Claude Code session. No API calls — just local file I/O.

Usage (stdio transport, registered in ~/.claude/settings.json):
    uv run python scripts/mcp_server.py
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path

from mcp.server.fastmcp import FastMCP
from article_schema import StoreValidationError
import kb_db
from locking import file_lock
from utils import safe_join

# Resolve paths relative to this file so it works from any cwd
ROOT_DIR = Path(__file__).resolve().parent.parent
KNOWLEDGE_DIR = ROOT_DIR / "knowledge"
CONCEPTS_DIR = KNOWLEDGE_DIR / "concepts"
CONNECTIONS_DIR = KNOWLEDGE_DIR / "connections"
QA_DIR = KNOWLEDGE_DIR / "qa"
INDEX_FILE = KNOWLEDGE_DIR / "index.md"
DAILY_DIR = ROOT_DIR / "daily"

mcp = FastMCP("knowledge-base")

ARTICLE_DIRS = [CONCEPTS_DIR, CONNECTIONS_DIR, QA_DIR]

USAGE_FILE = ROOT_DIR / "scripts" / "usage.json"
USAGE_LOCK = ROOT_DIR / "scripts" / ".locks" / "usage.lock"


def _record_article_read(rel_path: str) -> None:
    """Best-effort read counter feeding session-start hub selection.

    Telemetry must never break the tool — any failure is swallowed.
    """
    try:
        if kb_db.canonical_store(ROOT_DIR) is not None:
            from memory_store import MemoryStore
            MemoryStore(ROOT_DIR).record_read(rel_path)
            return
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


def _list_articles() -> list[Path]:
    articles = []
    for d in ARTICLE_DIRS:
        if d.exists():
            articles.extend(sorted(d.glob("*.md")))
    return articles


@mcp.tool()
def list_articles(project: str | None = None) -> str:
    """List all knowledge base articles with summaries from the index."""
    if kb_db.canonical_store(ROOT_DIR) is not None:
        rows = kb_db.article_records(ROOT_DIR, project)
        return ("# Knowledge Base Index (canonical SQLite)\n\n"
                "| Article | Summary | Updated |\n|---|---|---|\n" + "\n".join(
                    f"| [[{a['path']}]] | {a['summary']} | {a['updated']} |" for a in rows))
    if INDEX_FILE.exists():
        return INDEX_FILE.read_text(encoding="utf-8")
    return "Knowledge base is empty — no articles compiled yet."


@mcp.tool()
def search_knowledge(query: str, project: str | None = None, mode: str = "hybrid") -> str:
    """Search article text with optional project filtering and local semantic ranks."""
    if mode not in {"bm25", "hybrid"}:
        return "Invalid mode: use bm25 or hybrid."
    canonical = kb_db.canonical_store(ROOT_DIR) is not None
    backend = "bm25"
    if canonical or project is not None:
        results = kb_db.search(query, limit=10, project=project, root=ROOT_DIR)
    else:
        results = kb_db.search(query, limit=10)
    if mode == "hybrid":
        from semantic_search import SemanticUnavailable, hybrid_search
        try:
            results = hybrid_search(query, kb_db.article_records(ROOT_DIR, project), root=ROOT_DIR, limit=10)
            backend = "hybrid:bm25+multilingual-e5-small"
        except SemanticUnavailable as exc:
            backend = f"bm25 (semantic unavailable: {exc})"
    if results is None:
        # index missing/broken -> degrade to the linear scan
        return f"Backend: legacy-scan; {backend}\n\n" + _legacy_search(query)
    if not results:
        return f"Backend: {backend}\nNo articles matching '{query}'. Use list_articles() to see what's available."
    blocks = [
        f"### [[{r['path']}]] — {r['title']}\n"
        f"_{r['summary']}_ (updated {r['updated']})\n> {r['snippet']}"
        for r in results
    ]
    return f"Backend: {backend}\nFound {len(results)} matching articles:\n\n" + "\n\n".join(blocks)


def _legacy_search(query: str) -> str:
    query_lower = query.lower()
    keywords = query_lower.split()
    results: list[tuple[int, str]] = []

    for article in _list_articles():
        content = article.read_text(encoding="utf-8")
        content_lower = content.lower()
        rel = article.relative_to(KNOWLEDGE_DIR)

        # Extract title from frontmatter (needed for scoring)
        title = str(rel)
        title_match = re.search(r"^title:\s*(.+)$", content, re.MULTILINE)
        if title_match:
            title = title_match.group(1).strip().strip('"')
        title_lower = title.lower()

        # Score: capped keyword frequency + a strong title-hit bonus
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

        # Extract matching lines for context (up to 5)
        lines = content.split("\n")
        matching = []
        for i, line in enumerate(lines):
            if any(kw in line.lower() for kw in keywords):
                matching.append(line.strip())
            if len(matching) >= 5:
                break

        results.append((score, f"### [[{rel}]] — {title}\n**Relevance:** {matched}/{len(keywords)} keywords (score {score})\n" + "\n".join(f"> {m}" for m in matching)))

    if not results:
        return f"No articles matching '{query}'. Use list_articles() to see what's available."

    results.sort(key=lambda x: x[0], reverse=True)
    return f"Found {len(results)} matching articles:\n\n" + "\n\n".join(r[1] for r in results)


@mcp.tool()
def read_article(path: str) -> str:
    """Read a specific knowledge base article. Path is relative to knowledge/ dir (e.g. 'concepts/gpt-oss-20b.md')."""
    # Normalize: add .md if missing
    if not path.endswith(".md"):
        path += ".md"

    store = kb_db.canonical_store(ROOT_DIR)
    if store is not None:
        # Canonical identities use lexical validation, independent of exports.
        try:
            record = store.read_article(path)
        except StoreValidationError:
            return f"Invalid article path: {path}"
        if record is None:
            slug = Path(path).stem
            candidates = [a for a in store.list_articles() if slug in Path(a["path"]).name]
            if len(candidates) != 1:
                return f"Article not found: {path}. Use list_articles() to see available articles."
            record = candidates[0]
        _record_article_read(record["path"])
        return record["body"]

    article = safe_join(KNOWLEDGE_DIR, path)
    if article is None:
        return f"Invalid article path: {path}"

    if not article.exists():
        # Try fuzzy match
        slug = Path(path).stem
        for a in _list_articles():
            if slug in a.stem:
                _record_article_read(
                    str(a.relative_to(KNOWLEDGE_DIR)).removesuffix(".md").replace("\\", "/")
                )
                return f"(Did you mean {a.relative_to(KNOWLEDGE_DIR)}?)\n\n" + a.read_text(encoding="utf-8")
        return f"Article not found: {path}. Use list_articles() to see available articles."

    _record_article_read(
        str(article.relative_to(KNOWLEDGE_DIR)).removesuffix(".md").replace("\\", "/")
    )
    return article.read_text(encoding="utf-8")


@mcp.tool()
def read_source(path: str) -> str:
    """Read exact source provenance, including daily/archive paths, from canonical storage."""
    normalized = path.replace("\\", "/")
    if not normalized.startswith("daily/") or ".." in Path(normalized).parts:
        return f"Invalid source path: {path}"
    store = kb_db.canonical_store(ROOT_DIR)
    if store is not None:
        content = store.read_source(normalized)
    else:
        source = safe_join(DAILY_DIR, normalized.removeprefix("daily/"))
        content = source.read_text(encoding="utf-8") if source and source.is_file() else None
    return content if content is not None else f"Source not found: {path}"


@mcp.tool()
def search_daily_logs(query: str, last_n_days: int = 7, include_archive: bool = True) -> str:
    """Search daily logs including archives; last_n_days=0 searches all stored dates."""
    if last_n_days < 0:
        return "last_n_days must be nonnegative; use 0 for all dates."
    store = kb_db.canonical_store(ROOT_DIR)
    if store is not None:
        logs = store.list_sources(include_archive=include_archive)
    else:
        paths = list(DAILY_DIR.glob("*.md"))
        if include_archive:
            paths.extend((DAILY_DIR / "archive").rglob("*.md"))
        logs = [{"path": "daily/" + p.relative_to(DAILY_DIR).as_posix(),
                 "content": p.read_text(encoding="utf-8")} for p in paths]
    logs.sort(key=lambda row: (Path(row["path"]).name, row["path"]), reverse=True)
    if last_n_days:
        dates = sorted({Path(row["path"]).stem for row in logs}, reverse=True)[:last_n_days]
        logs = [row for row in logs if Path(row["path"]).stem in dates]
    if not logs:
        return "No daily logs found."

    query_lower = query.lower()
    keywords = query_lower.split()
    results: list[str] = []

    for log in logs:
        content = log["content"]
        content_lower = content.lower()

        if not any(kw in content_lower for kw in keywords):
            continue

        # Extract matching sections
        sections = re.split(r"^### ", content, flags=re.MULTILINE)
        matching_sections = []
        for section in sections:
            if any(kw in section.lower() for kw in keywords):
                # Truncate long sections
                if len(section) > 500:
                    section = section[:500] + "..."
                matching_sections.append("### " + section.strip())

        if matching_sections:
            results.append(f"## [[{log['path']}]]\n\n" + "\n\n".join(matching_sections[:3]))

    if not results:
        return f"No daily logs matching '{query}' in the last {last_n_days} days."

    return f"Found matches in {len(results)} daily logs:\n\n" + "\n\n---\n\n".join(results)


if __name__ == "__main__":
    mcp.run(transport="stdio")
