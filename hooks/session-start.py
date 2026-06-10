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
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Paths relative to project root
ROOT = Path(__file__).resolve().parent.parent
KNOWLEDGE_DIR = ROOT / "knowledge"
DAILY_DIR = ROOT / "daily"
INDEX_FILE = KNOWLEDGE_DIR / "index.md"

MAX_CONTEXT_CHARS = 20_000
MAX_LOG_LINES = 30
RECENT_DAYS = 14
MAX_HUB_ROWS = 15

_ROW_RE = re.compile(
    r"^\|\s*(\[\[[^\]]+\]\])\s*\|\s*(.*?)\s*\|\s*(.*?)\s*\|\s*([0-9]{4}-[0-9]{2}-[0-9]{2})\s*\|\s*$"
)


def get_recent_log() -> str:
    """Read the most recent daily log (today or yesterday)."""
    today = datetime.now(timezone.utc).astimezone()

    for offset in range(2):
        date = today - timedelta(days=offset)
        log_path = DAILY_DIR / f"{date.strftime('%Y-%m-%d')}.md"
        if log_path.exists():
            lines = log_path.read_text(encoding="utf-8").splitlines()
            # Return last N lines to keep context small
            recent = lines[-MAX_LOG_LINES:] if len(lines) > MAX_LOG_LINES else lines
            return "\n".join(recent)

    return "(no recent daily log)"


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
) -> tuple[list[dict], list[dict]]:
    """Split rows into (recent, hubs).

    recent — updated within recent_days, newest first.
    hubs — top-N remaining rows by source count (long-lived accumulating
    topics), most sources first. Rows already in recent are excluded.
    """
    cutoff = (now - timedelta(days=recent_days)).strftime("%Y-%m-%d")

    recent = [r for r in rows if r["updated"] >= cutoff]
    recent.sort(key=lambda r: r["updated"], reverse=True)
    recent_links = {r["link"] for r in recent}

    remaining = [r for r in rows if r["link"] not in recent_links]
    remaining.sort(key=lambda r: (-r["source_count"], r["link"]))
    hubs = [r for r in remaining if r["source_count"] >= 2][:max_hubs]

    return recent, hubs


def _format_row(row: dict) -> str:
    return f"| {row['link']} | {row['summary']} | {row['updated']} |"


def build_kb_section(rows: list[dict], now: datetime, budget: int) -> str:
    """Build the tiered KB section within a character budget.

    Priority: recent rows (newest first), then hub rows. Rows that don't
    fit are dropped whole — never truncated mid-row.
    """
    total = len(rows)
    recent, hubs = select_tier_rows(rows, now)

    header = (
        f"## Knowledge Base (tiered view: {total} articles total)\n\n"
        "Recently updated + long-lived hub articles below. The FULL index is at\n"
        f"`{INDEX_FILE}` — grep it (or the `knowledge-base` MCP tools: "
        "search_knowledge, read_article, list_articles) for anything not shown here.\n\n"
        "| Article | Summary | Updated |\n|---|---|---|\n"
    )
    hub_header = "\n**Hub articles (most-compiled long-lived topics):**\n\n| Article | Summary | Updated |\n|---|---|---|\n"

    used = len(header)
    recent_lines: list[str] = []
    for row in recent:
        line = _format_row(row) + "\n"
        if used + len(line) > budget:
            break
        recent_lines.append(line)
        used += len(line)

    hub_lines: list[str] = []
    if hubs and used + len(hub_header) < budget:
        used += len(hub_header)
        for row in hubs:
            line = _format_row(row) + "\n"
            if used + len(line) > budget:
                break
            hub_lines.append(line)
            used += len(line)

    section = header + "".join(recent_lines)
    if hub_lines:
        section += hub_header + "".join(hub_lines)
    return section


def build_context() -> str:
    """Assemble the context to inject into the conversation."""
    now = datetime.now(timezone.utc).astimezone()
    parts = [f"## Today\n{now.strftime('%A, %B %d, %Y')}"]

    # Recent daily log FIRST — it must always survive the budget.
    recent_log = get_recent_log()
    parts.append(f"## Recent Daily Log\n\n{recent_log}")

    # Tiered knowledge base view in whatever budget remains.
    if INDEX_FILE.exists():
        fixed = "\n\n---\n\n".join(parts)
        budget = MAX_CONTEXT_CHARS - len(fixed) - 16  # separator slack
        rows = parse_index_rows(INDEX_FILE.read_text(encoding="utf-8"))
        if rows and budget > 500:
            parts.append(build_kb_section(rows, now, budget))
        elif budget > 100:
            parts.append(
                f"## Knowledge Base\n\nIndex at `{INDEX_FILE}` — grep it or use "
                "the knowledge-base MCP tools (search_knowledge, read_article)."
            )
    else:
        parts.append("## Knowledge Base Index\n\n(empty - no articles compiled yet)")

    context = "\n\n---\n\n".join(parts)

    # Safety net only; the budgeter above should keep us under the cap.
    if len(context) > MAX_CONTEXT_CHARS:
        context = context[:MAX_CONTEXT_CHARS] + "\n\n...(truncated)"

    return context


def main():
    context = build_context()

    output = {
        "hookSpecificOutput": {
            "hookEventName": "SessionStart",
            "additionalContext": context,
        }
    }

    print(json.dumps(output))


if __name__ == "__main__":
    main()
