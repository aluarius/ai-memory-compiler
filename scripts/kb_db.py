"""SQLite + FTS5 sidecar index over the markdown knowledge base.

Markdown stays the source of truth; this database is derived and disposable —
rebuilt after every compile and in nightly maintenance. It exists because the
two hottest read paths outgrew flat files: MCP search was a linear scan of
400+ articles with naive keyword counting, and the compile prompt carried the
entire 111KB index (~33k tokens) on every run. FTS5/BM25 solves both: ranked
search with snippets, and a small relevance-selected index slice for compile.

Failure contract: every consumer falls back to the flat-file behavior when
the DB is missing or broken — a bad index can slow things down, never break
them.

Usage:
    uv run python scripts/kb_db.py rebuild
    uv run python scripts/kb_db.py search "nginx rsync"
"""

from __future__ import annotations

import argparse
import re
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from config import INDEX_FILE, KNOWLEDGE_DIR, SCRIPTS_DIR
from utils import INDEX_ROW_RE, list_wiki_articles

DB_FILE = SCRIPTS_DIR / "kb-index.sqlite"

# BM25 column weights: a query term in the title or curated summary says far
# more about relevance than one buried in the body.
_BM25_WEIGHTS = "10.0, 5.0, 1.0"

_TITLE_RE = re.compile(r'^title:\s*"?(.*?)"?\s*$', re.MULTILINE)
_WORD_RE = re.compile(r"\w+", re.UNICODE)

# compile_index_slice knobs
RECENT_DAYS = 14
MAX_RECENT = 60
MAX_CANDIDATES = 40
MAX_HUBS = 15
CANDIDATE_TERMS = 30


def _connect(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, timeout=5.0)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def _index_rows() -> dict[str, dict]:
    """index.md rows keyed by target: {summary, sources, updated, source_count}."""
    rows: dict[str, dict] = {}
    if not INDEX_FILE.exists():
        return rows
    for line in INDEX_FILE.read_text(encoding="utf-8").splitlines():
        m = INDEX_ROW_RE.match(line.strip())
        if not m:
            continue
        target, summary, sources, updated = m.groups()
        rows[target] = {
            "summary": summary,
            "updated": updated,
            "source_count": sources.count(",") + 1 if sources.strip() else 0,
        }
    return rows


def rebuild_index(db_path: Path | None = None) -> int:
    """Rebuild the whole index in one transaction. Returns article count.

    DELETE+INSERT (not DROP) so the long-lived MCP reader never sees a
    missing table; WAL keeps readers unblocked during the write.
    """
    db_path = db_path or DB_FILE
    index_rows = _index_rows()
    conn = _connect(db_path)
    try:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS articles ("
            " path TEXT PRIMARY KEY, title TEXT NOT NULL,"
            " summary TEXT NOT NULL DEFAULT '', updated TEXT NOT NULL DEFAULT '',"
            " source_count INTEGER NOT NULL DEFAULT 0)"
        )
        conn.execute(
            "CREATE VIRTUAL TABLE IF NOT EXISTS articles_fts USING fts5("
            " title, summary, body, path UNINDEXED, tokenize='unicode61')"
        )
        count = 0
        with conn:
            conn.execute("DELETE FROM articles")
            conn.execute("DELETE FROM articles_fts")
            for article in list_wiki_articles():
                rel = (
                    str(article.relative_to(KNOWLEDGE_DIR))
                    .removesuffix(".md")
                    .replace("\\", "/")
                )
                body = article.read_text(encoding="utf-8")
                title_match = _TITLE_RE.search(body)
                title = title_match.group(1) if title_match else rel
                meta = index_rows.get(rel, {})
                conn.execute(
                    "INSERT INTO articles VALUES (?, ?, ?, ?, ?)",
                    (
                        rel,
                        title,
                        meta.get("summary", ""),
                        meta.get("updated", ""),
                        meta.get("source_count", 0),
                    ),
                )
                conn.execute(
                    "INSERT INTO articles_fts (title, summary, body, path)"
                    " VALUES (?, ?, ?, ?)",
                    (title, meta.get("summary", ""), body, rel),
                )
                count += 1
        return count
    finally:
        conn.close()


def _fts_query(text: str, max_terms: int | None = None) -> str | None:
    """Turn free text into a safe FTS5 OR-query; None when no word tokens."""
    terms = _WORD_RE.findall(text.lower())
    if max_terms:
        # keep the most distinctive terms: prefer longer, then more frequent
        freq: dict[str, int] = {}
        for t in terms:
            if len(t) >= 4:
                freq[t] = freq.get(t, 0) + 1
        terms = sorted(freq, key=lambda t: (-freq[t], -len(t)))[:max_terms]
    seen: list[str] = []
    for t in terms:
        if t not in seen:
            seen.append(t)
    if not seen:
        return None
    return " OR ".join(f'"{t}"' for t in seen)


def search(
    query: str, limit: int = 10, db_path: Path | None = None
) -> list[dict] | None:
    """BM25-ranked search. None = DB unusable (caller falls back), [] = no hits."""
    db_path = db_path or DB_FILE
    if not Path(db_path).exists():
        return None
    fts_query = _fts_query(query)
    if fts_query is None:
        return []
    try:
        conn = _connect(db_path)
        try:
            rows = conn.execute(
                "SELECT f.path, a.title, a.summary, a.updated,"
                f" snippet(articles_fts, 2, '«', '»', '…', 12)"
                " FROM articles_fts f JOIN articles a ON a.path = f.path"
                f" WHERE articles_fts MATCH ?"
                f" ORDER BY bm25(articles_fts, {_BM25_WEIGHTS})"
                " LIMIT ?",
                (fts_query, limit),
            ).fetchall()
        finally:
            conn.close()
    except sqlite3.Error:
        return None
    return [
        {
            "path": path,
            "title": title,
            "summary": summary,
            "updated": updated,
            "snippet": snippet,
        }
        for path, title, summary, updated, snippet in rows
    ]


def _today() -> str:
    return datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d")


def compile_index_slice(
    log_text: str,
    db_path: Path | None = None,
    recent_days: int = RECENT_DAYS,
    max_recent: int = MAX_RECENT,
    max_candidates: int = MAX_CANDIDATES,
    max_hubs: int = MAX_HUBS,
) -> str | None:
    """Relevance-selected index slice for the compile prompt; None on failure.

    Three tiers, deduplicated in order: recently updated rows (the articles a
    new day most likely extends), FTS candidates matched against the day-log
    content, and the biggest hub rows by compiled-source count.
    """
    db_path = db_path or DB_FILE
    if not Path(db_path).exists():
        return None
    try:
        conn = _connect(db_path)
        try:
            cutoff = (
                datetime.fromisoformat(_today()) - timedelta(days=recent_days)
            ).strftime("%Y-%m-%d")
            recent = conn.execute(
                "SELECT path, summary, updated FROM articles WHERE updated >= ?"
                " ORDER BY updated DESC LIMIT ?",
                (cutoff, max_recent),
            ).fetchall()

            candidates: list[tuple] = []
            fts_query = _fts_query(log_text, max_terms=CANDIDATE_TERMS)
            if fts_query:
                candidates = conn.execute(
                    "SELECT f.path, a.summary, a.updated"
                    " FROM articles_fts f JOIN articles a ON a.path = f.path"
                    " WHERE articles_fts MATCH ?"
                    f" ORDER BY bm25(articles_fts, {_BM25_WEIGHTS})"
                    " LIMIT ?",
                    (fts_query, max_candidates),
                ).fetchall()

            hubs = conn.execute(
                "SELECT path, summary, updated FROM articles"
                " WHERE source_count >= 2 ORDER BY source_count DESC LIMIT ?",
                (max_hubs,),
            ).fetchall()
        finally:
            conn.close()
    except sqlite3.Error:
        return None

    seen: set[str] = set()
    lines: list[str] = []
    for path, summary, updated in [*recent, *candidates, *hubs]:
        if path in seen:
            continue
        seen.add(path)
        lines.append(f"| [[{path}]] | {summary} | {updated} |")

    total = len(_index_rows())
    return (
        f"RELEVANT SLICE of the index ({len(seen)} of {total} articles): recently"
        " updated, matched against this daily log, and major hubs. The FULL index"
        f" is at {INDEX_FILE} — before creating a NEW article, Grep it (and"
        " knowledge/concepts/) for existing coverage; this slice is not"
        " exhaustive.\n\n"
        "| Article | Summary | Updated |\n|---|---|---|\n" + "\n".join(lines)
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Manage the KB FTS index")
    parser.add_argument("command", choices=["rebuild", "search"])
    parser.add_argument("query", nargs="?", default="")
    args = parser.parse_args()

    if args.command == "rebuild":
        count = rebuild_index()
        print(f"Indexed {count} articles into {DB_FILE.name}")
        return 0

    results = search(args.query)
    if results is None:
        print("Index missing — run: uv run python scripts/kb_db.py rebuild")
        return 1
    for r in results:
        print(f"{r['path']}  [{r['updated']}]  {r['snippet']}")
    print(f"{len(results)} result(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
