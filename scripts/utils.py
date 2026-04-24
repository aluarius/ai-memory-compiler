"""Shared utilities for the personal knowledge base."""

import hashlib
import json
import re
from datetime import datetime
from pathlib import Path
from typing import Callable

from config import (
    CONCEPTS_DIR,
    CONNECTIONS_DIR,
    DAILY_DIR,
    DAILY_ARCHIVE_DIR,
    INDEX_FILE,
    KNOWLEDGE_DIR,
    LOCKS_DIR,
    LOG_FILE,
    QA_DIR,
    STATE_FILE,
)
from locking import file_lock


# ── State management ──────────────────────────────────────────────────

def load_state() -> dict:
    """Load persistent state from state.json."""
    lock_path = LOCKS_DIR / "state.lock"
    with file_lock(lock_path):
        if STATE_FILE.exists():
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        return {"ingested": {}, "query_count": 0, "last_lint": None, "total_cost": 0.0}


def save_state(state: dict) -> None:
    """Save state to state.json."""
    lock_path = LOCKS_DIR / "state.lock"
    with file_lock(lock_path):
        STATE_FILE.write_text(json.dumps(state, indent=2), encoding="utf-8")


def update_state(mutator: Callable[[dict], None]) -> dict:
    """Atomically load, mutate, and save state.json."""
    lock_path = LOCKS_DIR / "state.lock"
    with file_lock(lock_path):
        if STATE_FILE.exists():
            state = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        else:
            state = {"ingested": {}, "query_count": 0, "last_lint": None, "total_cost": 0.0}

        mutator(state)
        STATE_FILE.write_text(json.dumps(state, indent=2), encoding="utf-8")
        return state


# ── File hashing ──────────────────────────────────────────────────────

def file_hash(path: Path) -> str:
    """SHA-256 hash of a file (first 16 hex chars)."""
    return hashlib.sha256(path.read_bytes()).hexdigest()[:16]


# ── Slug / naming ─────────────────────────────────────────────────────

def slugify(text: str) -> str:
    """Convert text to a filename-safe slug."""
    text = text.lower().strip()
    text = re.sub(r"[^\w\s-]", "", text)
    text = re.sub(r"[\s_]+", "-", text)
    text = re.sub(r"-+", "-", text)
    return text.strip("-")


# ── Wikilink helpers ──────────────────────────────────────────────────

def strip_markdown_code(content: str) -> str:
    """Remove fenced and inline code spans before structural markdown parsing."""
    content = re.sub(r"```[\s\S]*?```", "", content)
    content = re.sub(r"~~~[\s\S]*?~~~", "", content)
    content = re.sub(r"`[^`\n]*`", "", content)
    return content


def extract_wikilinks(content: str) -> list[str]:
    """Extract all [[wikilinks]] from markdown content."""
    return re.findall(r"\[\[([^\]]+)\]\]", strip_markdown_code(content))


def wiki_article_exists(link: str) -> bool:
    """Check if a wikilinked article exists on disk."""
    path = KNOWLEDGE_DIR / f"{link}.md"
    return path.exists()


def resolve_daily_source(link: str) -> Path | None:
    """Resolve a daily source wikilink across active and archived logs."""
    normalized = link.removesuffix(".md").strip("/")
    if normalized.startswith("daily/archive/"):
        candidates = [DAILY_ARCHIVE_DIR / f"{normalized.removeprefix('daily/archive/')}.md"]
    elif normalized.startswith("daily/"):
        name = normalized.removeprefix("daily/")
        candidates = [
            DAILY_DIR / f"{name}.md",
            DAILY_ARCHIVE_DIR / f"{name}.md",
        ]
    else:
        candidates = [
            DAILY_DIR / f"{normalized}.md",
            DAILY_ARCHIVE_DIR / f"{normalized}.md",
        ]

    for candidate in candidates:
        if candidate.exists():
            return candidate
    return None


def daily_source_exists(link: str) -> bool:
    """Check if a daily log wikilink exists on disk."""
    return resolve_daily_source(link) is not None


def safe_join(root: Path, relative_path: str) -> Path | None:
    """Safely join an untrusted relative path under a trusted root."""
    candidate = (root / relative_path).resolve()
    root_resolved = root.resolve()

    try:
        candidate.relative_to(root_resolved)
    except ValueError:
        return None
    return candidate


# ── Wiki content helpers ──────────────────────────────────────────────

def read_wiki_index() -> str:
    """Read the knowledge base index file."""
    if INDEX_FILE.exists():
        return INDEX_FILE.read_text(encoding="utf-8")
    return "# Knowledge Base Index\n\n| Article | Summary | Compiled From | Updated |\n|---------|---------|---------------|---------|"


def list_indexed_articles(index_content: str | None = None) -> set[str]:
    """Return all article paths referenced from knowledge/index.md."""
    if index_content is None:
        index_content = read_wiki_index()
    return {match.strip() for match in re.findall(r"\[\[([^\]]+)\]\]", index_content)}


def read_all_wiki_content() -> str:
    """Read index + all wiki articles into a single string for context."""
    parts = [f"## INDEX\n\n{read_wiki_index()}"]

    for subdir in [CONCEPTS_DIR, CONNECTIONS_DIR, QA_DIR]:
        if not subdir.exists():
            continue
        for md_file in sorted(subdir.glob("*.md")):
            rel = md_file.relative_to(KNOWLEDGE_DIR)
            content = md_file.read_text(encoding="utf-8")
            parts.append(f"## {rel}\n\n{content}")

    return "\n\n---\n\n".join(parts)


def list_wiki_articles() -> list[Path]:
    """List all wiki article files."""
    articles = []
    for subdir in [CONCEPTS_DIR, CONNECTIONS_DIR, QA_DIR]:
        if subdir.exists():
            articles.extend(sorted(subdir.glob("*.md")))
    return articles


def find_unindexed_articles() -> list[str]:
    """List article paths present on disk but missing from knowledge/index.md."""
    indexed = list_indexed_articles()
    missing = []
    for article in list_wiki_articles():
        rel = str(article.relative_to(KNOWLEDGE_DIR)).replace(".md", "").replace("\\", "/")
        if rel not in indexed:
            missing.append(rel)
    return missing


def find_missing_index_targets() -> list[str]:
    """List index entries that point to missing article files."""
    missing = []
    for link in sorted(list_indexed_articles()):
        if not link.startswith(("concepts/", "connections/", "qa/")):
            continue
        if not (KNOWLEDGE_DIR / f"{link}.md").exists():
            missing.append(link)
    return missing


def list_raw_files() -> list[Path]:
    """List all daily log files."""
    if not DAILY_DIR.exists():
        return []
    return sorted(DAILY_DIR.glob("*.md"))


# ── Index helpers ─────────────────────────────────────────────────────

def count_inbound_links(target: str, exclude_file: Path | None = None) -> int:
    """Count how many wiki articles link to a given target."""
    count = 0
    for article in list_wiki_articles():
        if article == exclude_file:
            continue
        content = article.read_text(encoding="utf-8")
        if target in extract_wikilinks(content):
            count += 1
    return count


def get_article_word_count(path: Path) -> int:
    """Count words in an article, excluding YAML frontmatter."""
    content = path.read_text(encoding="utf-8")
    # Strip frontmatter
    if content.startswith("---"):
        end = content.find("---", 3)
        if end != -1:
            content = content[end + 3:]
    return len(content.split())


def build_index_entry(rel_path: str, summary: str, sources: str, updated: str) -> str:
    """Build a single index table row."""
    link = rel_path.replace(".md", "")
    return f"| [[{link}]] | {summary} | {sources} | {updated} |"


# ── Build log helpers ─────────────────────────────────────────────────

def normalize_build_log(content: str) -> str:
    """Sort build log entries chronologically while preserving the header block."""
    pattern = re.compile(r"^## \[([^\]]+)\].*?(?=^## \[|\Z)", re.MULTILINE | re.DOTALL)
    matches = list(pattern.finditer(content))
    if not matches:
        return content

    preamble = content[:matches[0].start()].rstrip()
    entries: list[tuple[datetime, int, str]] = []

    for idx, match in enumerate(matches):
        timestamp = datetime.fromisoformat(match.group(1))
        block = match.group(0).rstrip()
        entries.append((timestamp, idx, block))

    entries.sort(key=lambda item: (item[0], item[1]))
    ordered_blocks = "\n\n".join(block for _, _, block in entries)

    if preamble:
        return f"{preamble}\n\n{ordered_blocks}\n"
    return f"{ordered_blocks}\n"


def normalize_build_log_file(path: Path = LOG_FILE) -> bool:
    """Normalize knowledge/log.md in place if entries are out of order."""
    if not path.exists():
        return False

    original = path.read_text(encoding="utf-8")
    normalized = normalize_build_log(original)
    if normalized == original:
        return False

    path.write_text(normalized, encoding="utf-8")
    return True
