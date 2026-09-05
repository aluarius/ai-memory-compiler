"""
SessionStart hook - injects knowledge base context into every conversation.

This is the "context injection" layer. When Claude Code starts a session,
this hook injects the recent daily log plus a *tiered* slice of the knowledge
base index, so Claude always "remembers" what it has learned recently.

The full index outgrew the context budget long ago (100KB+ vs a 20KB cap),
so instead of truncating mid-table we select:
  1. articles updated in the last RECENT_DAYS days (newest first), then
  2. "hub" articles with the most compiled sources (long-lived topics),
and point the agent at the full index + MCP search tools for everything else.

Configure in .claude/settings.json:
{
    "hooks": {
        "SessionStart": [{
            "matcher": "",
            "command": "uv run python hooks/session-start.py"
        }]
    }
}
"""

import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Paths relative to project root
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import kb_db
KNOWLEDGE_DIR = ROOT / "knowledge"
DAILY_DIR = ROOT / "daily"
INDEX_FILE = KNOWLEDGE_DIR / "index.md"
USAGE_FILE = ROOT / "scripts" / "usage.json"

# An article read this many times via the MCP server qualifies as a hub even
# with a single compiled source — actual usage beats compile-count heuristics.
MIN_HUB_READS = 2

# Claude Code persists hook outputs larger than ~10KB to a file instead of
# inlining them (the model then sees only a 2KB preview + path). Observed on
# 19.4-19.5KB payloads across multiple sessions. Stay under that threshold —
# an inline 9.5KB beats a persisted 20KB.
MAX_CONTEXT_CHARS = 9_500
MAX_CONTEXT_BYTES = 9_500
MAX_LOG_BYTES = 2_000
MAX_LOG_LINES = 30
RECENT_DAYS = 14
MAX_HUB_ROWS = 15

_ROW_RE = re.compile(
    r"^\|\s*(\[\[[^\]]+\]\])\s*\|\s*(.*?)\s*\|\s*(.*?)\s*\|\s*([0-9]{4}-[0-9]{2}-[0-9]{2})\s*\|\s*$"
)


def is_internal_invocation() -> bool:
    """Return whether a service-side Codex/Claude call triggered this hook."""
    return bool(os.environ.get("CLAUDE_INVOKED_BY") or os.environ.get("MEMORY_COMPILER_INTERNAL"))


def get_recent_log() -> str:
    """Read the most recent daily log (today or yesterday)."""
    today = datetime.now(timezone.utc).astimezone()

    store = kb_db.canonical_store(ROOT)
    for offset in range(2):
        date = today - timedelta(days=offset)
        log_path = DAILY_DIR / f"{date.strftime('%Y-%m-%d')}.md"
        content = (store.read_source(f"daily/{log_path.name}") if store is not None
                   else log_path.read_text(encoding="utf-8") if log_path.exists() else None)
        if content is not None:
            lines = content.splitlines()
            # Return last N lines to keep context small
            recent = lines[-MAX_LOG_LINES:] if len(lines) > MAX_LOG_LINES else lines
            return "\n".join(recent)

    return "(no recent daily log)"


def load_usage_counts() -> dict:
    """Article read counters written by the MCP server; {} when absent/corrupt."""
    store = kb_db.canonical_store(ROOT)
    if store is not None:
        return store.usage_counts()
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


def parse_index_rows(index_text: str) -> list[dict]:
    """Parse the index.md table into row dicts.

    Each row: {link, summary, sources, updated, source_count}.
    Header and separator rows are skipped.
    """
    rows = []
    for line in index_text.splitlines():
        m = _ROW_RE.match(line.strip())
        if not m:
            continue
        link, summary, sources, updated = m.groups()
        if link == "[[Article]]":  # defensive: header variants
            continue
        rows.append(
            {
                "link": link,
                "summary": summary,
                "sources": sources,
                "updated": updated,
                "source_count": sources.count(",") + 1 if sources.strip() else 0,
            }
        )
    return rows


def select_tier_rows(
    rows: list[dict],
    now: datetime,
    recent_days: int = RECENT_DAYS,
    max_hubs: int = MAX_HUB_ROWS,
    usage: dict | None = None,
) -> tuple[list[dict], list[dict]]:
    """Split rows into (recent, hubs).

    recent — updated within recent_days, newest first.
    hubs — top-N remaining rows ranked by MCP read count, then source count
    (long-lived accumulating topics). A row qualifies through either signal.
    Rows already in recent are excluded.
    """
    cutoff = (now - timedelta(days=recent_days)).strftime("%Y-%m-%d")
    usage = usage or {}

    def reads(row: dict) -> int:
        return usage.get(row["link"].strip("[]"), 0)

    recent = [r for r in rows if r["updated"] >= cutoff or r.get("project_match")]
    recent.sort(key=lambda r: (bool(r.get("project_match")), r["updated"]), reverse=True)
    recent_links = {r["link"] for r in recent}

    remaining = [r for r in rows if r["link"] not in recent_links]
    remaining.sort(key=lambda r: (-reads(r), -r["source_count"], r["link"]))
    hubs = [
        r for r in remaining
        if r["source_count"] >= 2 or reads(r) >= MIN_HUB_READS
    ][:max_hubs]

    return recent, hubs


def _format_row(row: dict) -> str:
    summary = " ".join(row["summary"].splitlines()).replace("|", "\\|")
    return f"| {row['link']} | {summary} | {row['updated']} |"


def build_kb_section(
    rows: list[dict], now: datetime, budget: int, usage: dict | None = None
) -> str:
    """Build complete rows within a UTF-8 budget, reserving space for hubs."""
    total = len(rows)
    recent, hubs = select_tier_rows(rows, now, usage=usage)

    header = (
        f"## Knowledge Base (tiered view: {total} articles total)\n\n"
        "Project matches, recent articles, and long-lived hubs below. Use the "
        "`knowledge-base` MCP tools search_knowledge, read_article, list_articles "
        "for complete canonical knowledge. An optional Markdown export is at "
        f"`{INDEX_FILE}`.\n\n"
        "| Article | Summary | Updated |\n|---|---|---|\n"
    )
    hub_header = "\n**Hub articles (most-compiled long-lived topics):**\n\n| Article | Summary | Updated |\n|---|---|---|\n"

    if _bytes(header) > budget:
        return ""
    available = budget - _bytes(header)
    hub_lines = []
    if hubs:
        first_hub_size = _bytes(hub_header + _format_row(hubs[0]) + "\n")
        reserve = min(available, max(available // 3, first_hub_size))
        hub_lines = _fit_rows(hubs, reserve - _bytes(hub_header))
    reserved = _bytes(hub_header + "".join(hub_lines)) if hub_lines else 0
    recent_lines = _fit_rows(recent, available - reserved)

    section = header + "".join(recent_lines)
    if hub_lines:
        section += hub_header + "".join(hub_lines)
    return section


def _bytes(text: str) -> int:
    return len(text.encode("utf-8"))


def _fit_rows(rows: list[dict], budget: int) -> list[str]:
    lines = []
    for row in rows:
        line = _format_row(row) + "\n"
        size = _bytes(line)
        if size <= budget:
            lines.append(line)
            budget -= size
    return lines


def _project_rows(cwd: str | None) -> list[dict]:
    store = kb_db.canonical_store(ROOT)
    if store is None:
        return parse_index_rows(INDEX_FILE.read_text(encoding="utf-8")) if INDEX_FILE.exists() else []
    articles = store.list_articles()
    if cwd:
        articles = [a for a in articles if not a["projects"] or kb_db.matches_project(a, cwd)]
    return [{"link": f"[[{a['path']}]]", "summary": a["summary"],
             "sources": ", ".join(a["sources"]), "updated": a["updated"],
             "source_count": len(a["sources"]),
             "project_match": bool(cwd and kb_db.matches_project(a, cwd))} for a in articles]


def build_context(cwd: str | None = None) -> str:
    """Assemble the context to inject into the conversation."""
    now = datetime.now(timezone.utc).astimezone()
    parts = [f"## Today\n{now.strftime('%A, %B %d, %Y')}"]

    # Recent daily log FIRST — it must always survive the budget.
    recent_log = get_recent_log().encode("utf-8")[-MAX_LOG_BYTES:].decode("utf-8", errors="ignore")
    parts.append(f"## Recent Daily Log\n\n{recent_log}")

    # Tiered knowledge base view in whatever budget remains.
    rows = _project_rows(cwd)
    if rows:
        fixed = "\n\n---\n\n".join(parts)
        budget = MAX_CONTEXT_BYTES - _bytes(fixed) - 7
        if rows and budget > 500:
            parts.append(build_kb_section(rows, now, budget, load_usage_counts()))
        elif budget > 100:
            parts.append(
                f"## Knowledge Base\n\nIndex at `{INDEX_FILE}` — grep it or use "
                "the knowledge-base MCP tools (search_knowledge, read_article)."
            )
    else:
        parts.append("## Knowledge Base Index\n\n(empty - no articles compiled yet)")

    context = "\n\n---\n\n".join(parts)

    return context


def main() -> None:
    if is_internal_invocation():
        return
    try:
        payload = json.load(sys.stdin) if not sys.stdin.isatty() else {}
    except (ValueError, OSError):
        payload = {}
    cwd = payload.get("cwd") if isinstance(payload, dict) else None
    context = build_context(cwd if isinstance(cwd, str) else None)

    output = {
        "hookSpecificOutput": {
            "hookEventName": "SessionStart",
            "additionalContext": context,
        }
    }

    print(json.dumps(output, ensure_ascii=False))


if __name__ == "__main__":
    main()
