"""
MCP server for the personal knowledge base.

Exposes tools to search and read knowledge base articles from any
Claude Code session. No API calls — just local file I/O.

Usage (stdio transport, registered in ~/.claude/settings.json):
    uv run python scripts/mcp_server.py
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

from mcp.server.fastmcp import FastMCP

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


def _list_articles() -> list[Path]:
    articles = []
    for d in ARTICLE_DIRS:
        if d.exists():
            articles.extend(sorted(d.glob("*.md")))
    return articles


@mcp.tool()
def list_articles() -> str:
    """List all knowledge base articles with summaries from the index."""
    if INDEX_FILE.exists():
        return INDEX_FILE.read_text(encoding="utf-8")
    return "Knowledge base is empty — no articles compiled yet."


@mcp.tool()
def search_knowledge(query: str) -> str:
    """Search knowledge base articles by keyword. Returns matching excerpts with article paths."""
    query_lower = query.lower()
    keywords = query_lower.split()
    results: list[str] = []

    for article in _list_articles():
        content = article.read_text(encoding="utf-8")
        content_lower = content.lower()

        # Score: how many keywords match
        score = sum(1 for kw in keywords if kw in content_lower)
        if score == 0:
            continue

        rel = article.relative_to(KNOWLEDGE_DIR)

        # Extract title from frontmatter
        title = str(rel)
        title_match = re.search(r"^title:\s*(.+)$", content, re.MULTILINE)
        if title_match:
            title = title_match.group(1).strip().strip('"')

        # Extract matching lines for context (up to 5)
        lines = content.split("\n")
        matching = []
        for i, line in enumerate(lines):
            if any(kw in line.lower() for kw in keywords):
                matching.append(line.strip())
            if len(matching) >= 5:
                break

        results.append((score, f"### [[{rel}]] — {title}\n**Relevance:** {score}/{len(keywords)} keywords\n" + "\n".join(f"> {m}" for m in matching)))

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

    article = KNOWLEDGE_DIR / path
    if not article.exists():
        # Try fuzzy match
        slug = Path(path).stem
        for a in _list_articles():
            if slug in a.stem:
                return f"(Did you mean {a.relative_to(KNOWLEDGE_DIR)}?)\n\n" + a.read_text(encoding="utf-8")
        return f"Article not found: {path}. Use list_articles() to see available articles."

    return article.read_text(encoding="utf-8")


@mcp.tool()
def search_daily_logs(query: str, last_n_days: int = 7) -> str:
    """Search recent daily conversation logs by keyword. Useful for finding context from recent sessions."""
    if not DAILY_DIR.exists():
        return "No daily logs found."

    logs = sorted(DAILY_DIR.glob("*.md"), reverse=True)[:last_n_days]
    if not logs:
        return "No daily logs found."

    query_lower = query.lower()
    keywords = query_lower.split()
    results: list[str] = []

    for log in logs:
        content = log.read_text(encoding="utf-8")
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
            results.append(f"## {log.name}\n\n" + "\n\n".join(matching_sections[:3]))

    if not results:
        return f"No daily logs matching '{query}' in the last {last_n_days} days."

    return f"Found matches in {len(results)} daily logs:\n\n" + "\n\n---\n\n".join(results)


if __name__ == "__main__":
    mcp.run(transport="stdio")
