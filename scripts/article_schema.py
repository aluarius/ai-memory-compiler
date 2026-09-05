"""Validated article and source identifiers shared by storage and compilation."""

from __future__ import annotations

import re
from datetime import date, datetime
from typing import Any

import yaml

from utils import extract_wikilinks


class StoreValidationError(ValueError):
    """A proposed change violates the persistent knowledge contract."""


def article_path(value: str) -> str:
    """Return a safe, extensionless article identity, never a filesystem path."""
    value = value.removesuffix(".md")
    if not re.fullmatch(r"(?:concepts|connections|qa)/[a-z0-9][a-z0-9_.-]*", value):
        raise StoreValidationError(f"Invalid article path: {value}")
    return value


def source_path(value: str) -> str:
    """Canonicalize archival locations without changing the source identity."""
    match = re.fullmatch(r"daily/(?:archive/)?(\d{4}-\d{2}-\d{2})(?:\.md)?", value)
    if not match:
        raise StoreValidationError(f"Invalid source path: {value}")
    date.fromisoformat(match[1])
    return f"daily/{match[1]}.md"


def _date(value: Any, field: str) -> str:
    if isinstance(value, datetime):
        value = value.date()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, str):
        try:
            return date.fromisoformat(value).isoformat()
        except ValueError:
            pass
    raise StoreValidationError(f"Invalid article {field}: expected YYYY-MM-DD")


def parse_article(change: dict[str, Any]) -> dict[str, Any]:
    """Validate an article without modifying its original Markdown bytes."""
    if not isinstance(change, dict):
        raise StoreValidationError("Article changes must be objects")
    path = article_path(str(change.get("path", "")))
    body = change.get("body")
    if not isinstance(body, str):
        raise StoreValidationError(f"{path}: article body must be text")
    front = re.match(r"\A---\r?\n(.*?)\r?\n---(?:\r?\n|\Z)", body, re.DOTALL)
    if not front:
        raise StoreValidationError(f"{path}: missing YAML frontmatter")
    try:
        metadata = yaml.safe_load(front[1])
    except yaml.YAMLError as exc:
        raise StoreValidationError(f"{path}: malformed YAML frontmatter") from exc
    if not isinstance(metadata, dict):
        raise StoreValidationError(f"{path}: frontmatter must be a mapping")
    title = metadata.get("title")
    if not isinstance(title, str) or not title.strip():
        raise StoreValidationError(f"{path}: missing title")
    sources = metadata.get("sources")
    if not isinstance(sources, list) or not sources or any(not isinstance(s, str) for s in sources):
        raise StoreValidationError(f"{path}: sources must be a nonempty list of paths")
    summary = change.get("summary", title)
    if not isinstance(summary, str) or not summary.strip() or "\n" in summary or "|" in summary:
        raise StoreValidationError(f"{path}: summary must be a nonempty table-safe line")
    projects = change.get("projects", metadata.get("projects", []))
    if not isinstance(projects, list) or any(not isinstance(p, str) or not p.strip() for p in projects):
        raise StoreValidationError(f"{path}: projects must be a list of names or roots")
    links = []
    source_links = []
    for target in extract_wikilinks(body):
        target = target.split("#", 1)[0]
        if target.startswith("daily/"):
            source_links.append(source_path(target))
        elif target:
            links.append(article_path(target))
    return {
        "path": path, "title": title, "body": body, "summary": summary.strip(),
        "sources": list(dict.fromkeys(source_path(s) for s in sources)),
        "created": _date(metadata.get("created"), "created"),
        "updated": _date(metadata.get("updated"), "updated"),
        "projects": list(dict.fromkeys(projects)), "links": list(dict.fromkeys(links)),
        "source_links": list(dict.fromkeys(source_links)),
    }
