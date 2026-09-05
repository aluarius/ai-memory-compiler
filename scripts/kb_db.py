"""FTS5 retrieval over canonical article snapshots, with legacy read compatibility.

After migration MemoryStore owns all content. In-memory FTS snapshots remain
disposable and refresh when article content or metadata changes. An unmigrated
knowledge base may still use its old sidecar or Markdown files. Canonical store
errors never redirect reads to potentially stale exports.

Usage:
    uv run python scripts/kb_db.py rebuild
    uv run python scripts/kb_db.py search "nginx rsync"
"""

from __future__ import annotations

import argparse
import hashlib
import re
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING

from config import INDEX_FILE, KNOWLEDGE_DIR, SCRIPTS_DIR
from utils import INDEX_ROW_RE, list_wiki_articles

if TYPE_CHECKING:
    from memory_store import MemoryStore

DB_FILE = SCRIPTS_DIR / "kb-index.sqlite"

# BM25 column weights: a query term in the title or curated summary says far
# more about relevance than one buried in the body.
_BM25_WEIGHTS = "10.0, 5.0, 1.0"

_TITLE_RE = re.compile(r'^title:\s*"?(.*?)"?\s*$', re.MULTILINE)
_WORD_RE = re.compile(r"\w+", re.UNICODE)

# Function words are not relevance evidence in natural-language questions.
# In a mixed-language corpus, rare-language glue words otherwise receive high
# IDF and let unrelated articles crowd out exact technical terms. Keep this
# language-only list independent of projects and retrieval evaluation queries.
_FUNCTION_WORDS = frozenset("""
a about above after again against all am an and any are as at be because been
before being below between both but by can could did do does doing down during
each few for from further had has have having he her here hers herself him
himself his how i if in into is it its itself just me more most my myself no nor
not now of off on once only or other our ours ourselves out over own same she
should so some such than that the their theirs them themselves then there these
they this those through to too under until up very was we were what when where
which while who whom why will with would you your yours yourself yourselves
а без более бы был была были было быть в вам вас весь во вот все всего всех вы
где да даже для до его ее её если есть еще ещё же за зачем здесь и из или им
ими их к как какая какие какой когда кого кому который кто куда ли либо мне
мной мною может можно мои мой моя мы на надо нам нас наш не него нее неё нет
ни них но ну нужно о об обо он она они оно от перед по под почему при про
с сам сама сами самый свою себе себя со так такая такие такой там те тем то
того тоже той только том тому тот тут ты у уже уж чего чем через что чтобы
чья чьи чей чьё чье эта эти это этой этом этот я
""".split())

# compile_index_slice knobs
RECENT_DAYS = 14
MAX_RECENT = 60
MAX_CANDIDATES = 40
MAX_HUBS = 15
CANDIDATE_TERMS = 30


def canonical_store(root: Path | None = None) -> MemoryStore | None:
    """Return the canonical reader; only an unmigrated KB uses legacy files."""
    from memory_store import MemoryStore, is_initialized
    root = Path(root) if root is not None else KNOWLEDGE_DIR.parent
    if is_initialized(root):
        return MemoryStore(root, readonly=True)
    if (root / "scripts" / "memory.sqlite").exists():
        raise RuntimeError("Canonical memory database is not ready or is corrupt; Markdown fallback is disabled")
    return None


def matches_project(article: dict, project: str) -> bool:
    """Match a project slug or a cwd inside an explicitly associated root."""
    requested = Path(project).expanduser()
    for value in article.get("projects", []):
        associated = Path(value).expanduser()
        if value == project or associated.name == requested.name:
            return True
        if associated.is_absolute() and requested.is_absolute() and requested.is_relative_to(associated):
            return True
        if not associated.is_absolute() and value in requested.parts:
            return True
    return False


def article_records(root: Path | None = None, project: str | None = None) -> list[dict]:
    """Read canonical records, or labelled legacy compatibility before migration."""
    store = canonical_store(root)
    if store is not None:
        records = store.list_articles()
    else:
        knowledge = Path(root) / "knowledge" if root is not None else KNOWLEDGE_DIR
        index = knowledge / "index.md"
        metadata = {}
        if index.exists():
            for line in index.read_text(encoding="utf-8").splitlines():
                match = INDEX_ROW_RE.match(line.strip())
                if match:
                    path, summary, sources, updated = match.groups()
                    metadata[path] = (summary, sources, updated)
        records = []
        paths = (list_wiki_articles() if root is None else
                 [path for directory in ("concepts", "connections", "qa")
                  for path in sorted((knowledge / directory).glob("*.md"))])
        for path in paths:
            body = path.read_text(encoding="utf-8")
            rel = path.relative_to(knowledge).as_posix().removesuffix(".md")
            title = _TITLE_RE.search(body)
            summary, sources, updated = metadata.get(rel, ("", "", ""))
            records.append({"path": rel, "title": title.group(1) if title else rel,
                            "summary": summary, "body": body, "updated": updated,
                            "sources": [s.strip() for s in sources.split(",") if s.strip()],
                            "projects": [], "content_hash": hashlib.sha256(body.encode()).hexdigest(),
                            "revision": 0})
    return [a for a in records if matches_project(a, project)] if project else records


def _populate(conn: sqlite3.Connection, records: list[dict]) -> None:
    conn.execute("CREATE TABLE IF NOT EXISTS articles (path TEXT PRIMARY KEY, title TEXT NOT NULL,"
                 " summary TEXT NOT NULL, updated TEXT NOT NULL, source_count INTEGER NOT NULL)")
    conn.execute("CREATE VIRTUAL TABLE IF NOT EXISTS articles_fts USING fts5("
                 "title, summary, body, path UNINDEXED, tokenize='unicode61')")
    with conn:
        conn.execute("DELETE FROM articles")
        conn.execute("DELETE FROM articles_fts")
        conn.executemany("INSERT INTO articles VALUES (?, ?, ?, ?, ?)",
                         [(a["path"], a["title"], a["summary"], a["updated"], len(a["sources"]))
                          for a in records])
        conn.executemany("INSERT INTO articles_fts VALUES (?, ?, ?, ?)",
                         [(a["title"], a["summary"], a["body"], a["path"]) for a in records])


_snapshot = threading.local()


def search_records(query: str, records: list[dict], limit: int = 10) -> list[dict]:
    """Search a canonical snapshot without disk writes or export dependencies."""
    fts_query = _fts_query(query)
    if not fts_query or limit <= 0:
        return []
    fingerprint = tuple((a["path"], a["content_hash"], a["summary"], a["updated"]) for a in records)
    if getattr(_snapshot, "fingerprint", None) != fingerprint:
        previous = getattr(_snapshot, "conn", None)
        if previous is not None:
            previous.close()
        conn = sqlite3.connect(":memory:")
        _populate(conn, records)
        _snapshot.conn = conn
        _snapshot.fingerprint = fingerprint
    rows = _snapshot.conn.execute(
        "SELECT f.path, snippet(articles_fts, 2, '«', '»', '…', 12)"
        " FROM articles_fts f WHERE articles_fts MATCH ?"
        f" ORDER BY bm25(articles_fts, {_BM25_WEIGHTS}), f.path LIMIT ?", (fts_query, limit)).fetchall()
    by_path = {a["path"]: a for a in records}
    return [{**by_path[path], "snippet": snippet} for path, snippet in rows]


def _connect(db_path: Path, *, readonly: bool = False) -> sqlite3.Connection:
    if readonly:
        conn = sqlite3.connect(Path(db_path).resolve().as_uri() + "?mode=ro", uri=True, timeout=5.0)
    else:
        conn = sqlite3.connect(db_path, timeout=5.0)
    if not readonly:
        conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def _read_index(db_path: Path) -> sqlite3.Connection | None:
    store = canonical_store()
    if store is not None:
        conn = sqlite3.connect(":memory:")
        _populate(conn, store.list_articles())
        return conn
    return _connect(db_path, readonly=True) if Path(db_path).exists() else None


def rebuild_index(db_path: Path | None = None) -> int:
    """Rebuild the whole index in one transaction. Returns article count.

    DELETE+INSERT (not DROP) so the long-lived MCP reader never sees a
    missing table; WAL keeps readers unblocked during the write.
    """
    db_path = db_path or DB_FILE
    records = article_records()
    conn = _connect(db_path)
    try:
        _populate(conn, records)
        return len(records)
    finally:
        conn.close()


def _fts_query(text: str, max_terms: int | None = None) -> str | None:
    """Build a safe OR-query from meaningful terms; single-word searches stay literal."""
    original_terms = _WORD_RE.findall(text)
    terms = [term.lower() for term in original_terms]
    literal = {word.lower() for match in re.finditer(r'(["`])(.+?)\1', text)
               for word in _WORD_RE.findall(match[2])}
    # Keep explicit code keywords even inside a natural-language question.
    literal.update(word.lower() for word in original_terms if word.isascii() and word.isupper())
    if len(terms) > 1:
        terms = [term for term in terms if term not in _FUNCTION_WORDS or term in literal]
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
    query: str, limit: int = 10, db_path: Path | None = None, *,
    project: str | None = None, root: Path | None = None,
) -> list[dict] | None:
    """BM25-ranked search. None = DB unusable (caller falls back), [] = no hits."""
    if canonical_store(root) is not None or root is not None or project is not None:
        return search_records(query, article_records(root, project), limit)
    db_path = db_path or DB_FILE
    if not Path(db_path).exists():
        return None
    fts_query = _fts_query(query)
    if fts_query is None:
        return []
    try:
        conn = _connect(db_path, readonly=True)
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


PAIR_NEIGHBOURS = 5
PAIR_PROFILE_TERMS = 25


def find_similar_pairs(
    limit: int = 20,
    db_path: Path | None = None,
    neighbours: int = PAIR_NEIGHBOURS,
) -> list[dict] | None:
    """Mutually-nearest article pairs — the real consolidation candidates.

    Thin articles stopped being the growth problem long ago (the compiler
    schema forbids stubs, so nothing lands under the sparse threshold);
    what actually accumulates is *topic overlap* — two articles circling the
    same subject, sometimes without even linking to each other. Mutual
    nearest-neighbourhood in the FTS index is a cheap, embedding-free signal
    for exactly that: A must rank B highly AND B must rank A highly.

    Pair strength is the combined BM25 relevance of the two directed matches,
    not their rank positions: ranks bucket everything into a handful of values
    (observed: 3 distinct scores across 200 real pairs), which silently turns
    any top-N into an alphabetical slice.

    Returns pairs sorted by combined strength; `linked` says whether they
    already wikilink each other (link-only fixes are much cheaper than a
    merge). None when the index is unusable.
    """
    db_path = db_path or DB_FILE
    try:
        conn = _read_index(db_path)
        if conn is None:
            return None
        try:
            rows = conn.execute("SELECT path, title, summary FROM articles").fetchall()
            # path -> {neighbour: bm25 relevance (higher = closer)}
            neighbours_of: dict[str, dict[str, float]] = {}
            for path, title, summary in rows:
                profile = _fts_query(f"{title} {summary}", max_terms=PAIR_PROFILE_TERMS)
                if not profile:
                    continue
                hits = conn.execute(
                    "SELECT path, bm25(articles_fts, "
                    f"{_BM25_WEIGHTS}) AS relevance FROM articles_fts"
                    " WHERE articles_fts MATCH ? ORDER BY relevance LIMIT ?",
                    (profile, neighbours + 1),
                ).fetchall()
                neighbours_of[path] = {
                    other: -relevance  # bm25 is negative; flip so higher = closer
                    for other, relevance in hits
                    if other != path
                }
        finally:
            conn.close()
    except sqlite3.Error:
        return None

    pairs: list[dict] = []
    for a, near_a in neighbours_of.items():
        for b, relevance_ab in near_a.items():
            if b <= a:  # emit each unordered pair once
                continue
            relevance_ba = neighbours_of.get(b, {}).get(a)
            if relevance_ba is None:  # not mutual -> weak signal, skip
                continue
            score = round((relevance_ab + relevance_ba) / 2, 3)
            pairs.append({"a": a, "b": b, "score": score})

    pairs.sort(key=lambda p: (-p["score"], p["a"]))
    top = pairs[:limit]
    # link check reads both articles, so only pay for the pairs we return
    for pair in top:
        pair["linked"] = _pair_is_linked(pair["a"], pair["b"])
    return top


def _pair_is_linked(a: str, b: str) -> bool:
    """True if either article wikilinks the other."""
    store = canonical_store()
    for src, dst in ((a, b), (b, a)):
        if store is not None:
            article = store.read_article(src)
            if article and f"[[{dst}]]" in article["body"]:
                return True
            continue
        path = KNOWLEDGE_DIR / f"{src}.md"
        if path.exists() and f"[[{dst}]]" in path.read_text(encoding="utf-8"):
            return True
    return False


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
    try:
        conn = _read_index(db_path)
        if conn is None:
            return None
        try:
            total = conn.execute("SELECT COUNT(*) FROM articles").fetchone()[0]
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
