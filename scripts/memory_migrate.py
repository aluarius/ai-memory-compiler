"""Back up and import the legacy file corpus without deleting or rewriting it."""

from __future__ import annotations

import argparse
import json
import os
import re
import tarfile
import tempfile
from contextlib import ExitStack
from datetime import datetime
from pathlib import Path

from locking import file_lock
from memory_store import MemoryStore, StoreConflict, StoreValidationError, content_hash, timestamp
from utils import INDEX_ROW_RE

_RECOVERY_RE = re.compile(r"^(?:import-flush|session-flush|flush-context)-([0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12})(?:-(\d+)-(\d+|latest))?")
_STATE_FILES = {"state.json": "pipeline", "usage.json": "legacy_usage", ".last-codex-import.json": "codex_import", "last-flush.json": "legacy_flush"}


def recovery_metadata(path: Path) -> tuple[str, dict]:
    match = _RECOVERY_RE.match(path.name)
    if not match:
        return f"legacy:{path.name}", {"session_id": path.stem, "source": "legacy-recovery", "legacy_path": path.name}
    session, after, until = match.groups()
    metadata = {"session_id": session, "source": "legacy-recovery", "legacy_path": path.name,
                "agent": "codex" if path.name.startswith("import-flush-") else "claude_code",
                "provider": "openai" if path.name.startswith("import-flush-") else "anthropic"}
    identity = session
    captured = re.search(r"-(\d{8}-\d{6})\.md$", path.name)
    if captured:
        try:
            metadata["captured_at"] = datetime.strptime(captured[1], "%Y%m%d-%H%M%S").astimezone().isoformat()
        except ValueError:
            pass
    if after is not None:
        metadata.update({"after": int(after), "until": int(until) if until != "latest" else None})
        identity += f":{after}:{until}"
    return identity, metadata


def import_recovery_contexts(store: MemoryStore, source_root: Path) -> int:
    from capture_service import sanitize
    paths = set((source_root / "reports/failed-flushes").glob("*.md"))
    permanent = set((source_root / "reports/failed-flushes/permanent").glob("*.md"))
    for pattern in ("session-flush-*.md", "flush-context-*.md", "import-flush-*.md"):
        paths.update((source_root / "scripts").glob(pattern))
    imported = 0
    for path in sorted(paths | permanent):
        context = sanitize(path.read_bytes().decode("utf-8"))
        if not context.strip():
            continue
        identity, metadata = recovery_metadata(path)
        job_id = store.enqueue(context, metadata, identity=identity)
        if path in permanent:
            with store.transaction() as conn:
                conn.execute("UPDATE jobs SET status='quarantined' WHERE id=? AND status='pending'", (job_id,))
        imported += 1
    return imported


def _read_json(path: Path) -> dict:
    if not path.exists():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"Legacy state must be an object: {path}")
    return data


def _backup(root: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(destination, "x:gz") as archive:
        for relative in ("knowledge", "daily", "reports/failed-flushes", "reports/capture-spool"):
            path = root / relative
            if path.exists():
                archive.add(path, arcname=relative, recursive=True)
        for name in [*_STATE_FILES, "runtime-config.json"]:
            path = root / "scripts" / name
            if path.exists():
                archive.add(path, arcname=f"scripts/{name}")
        for pattern in ("session-flush-*.md", "flush-context-*.md", "import-flush-*.md"):
            for path in (root / "scripts").glob(pattern):
                archive.add(path, arcname=f"scripts/{path.name}")


def _infer_projects(path: str, body: str, projects: set[str]) -> list[str]:
    slug = Path(path).name
    return sorted(project for project in projects if slug.startswith(project + "-") or f"/{project}/" in body)


def migrate(root: Path, *, source_root: Path | None = None) -> dict:
    """Publish only a fully imported and validated database; retain all legacy files."""
    root = root.resolve()
    source_root = (source_root or root).resolve()
    root.joinpath("scripts").mkdir(parents=True, exist_ok=True)
    with ExitStack() as stack:
        stack.enter_context(file_lock(root / "scripts/.locks/migration.lock"))
        if root == source_root:
            for name in ("compile", "flush-llm", "daily-log", "codex-stop", "state", "flush-state"):
                stack.enter_context(file_lock(root / f"scripts/.locks/{name}.lock"))
        if MemoryStore.is_initialized(root):
            store = MemoryStore(root)
            return {"already_migrated": True, "articles": len(store.list_articles()), "sources": len(store.list_sources())}
        if (root / "scripts/memory.sqlite").exists():
            raise StoreConflict("An unrecognized memory.sqlite exists; preserve and inspect it before migrating")
        backup = root / "reports/migration-backups" / (timestamp().replace(":", "-") + "-legacy.tar.gz")
        _backup(source_root, backup)
        summaries = {}
        index = source_root / "knowledge/index.md"
        if index.exists():
            for line in index.read_text(encoding="utf-8").splitlines():
                match = INDEX_ROW_RE.match(line.strip())
                if match:
                    summaries[match[1]] = match[2]
        with tempfile.TemporaryDirectory(prefix="memory-migration-", dir=root / "scripts") as temporary:
            staged = MemoryStore(Path(temporary))
            staged.initialize()
            manifest = {}
            projects = set()
            source_count = 0
            for path in sorted((source_root / "daily").rglob("*.md")):
                body = path.read_bytes().decode("utf-8")
                relative = path.relative_to(source_root).as_posix()
                staged.import_source(relative, body, archived=path.parent.name == "archive")
                manifest[relative] = content_hash(body)
                for cwd in re.findall(r"\bcwd=([^|\n]+)", body):
                    name = Path(cwd.strip().rstrip("_ ")).name
                    if re.fullmatch(r"[a-z0-9][a-z0-9-]+", name):
                        projects.add(name)
                source_count += 1
            changes = []
            for directory in ("concepts", "connections", "qa"):
                for path in sorted((source_root / "knowledge" / directory).glob("*.md")):
                    body = path.read_bytes().decode("utf-8")
                    relative = path.relative_to(source_root / "knowledge").as_posix().removesuffix(".md")
                    changes.append({"path": relative, "body": body, "summary": summaries.get(relative, Path(relative).name),
                                    "projects": _infer_projects(relative, body, projects)})
                    manifest[f"knowledge/{relative}.md"] = content_hash(body)
            staged.commit_articles(changes)
            for name, namespace in _STATE_FILES.items():
                staged.set_state(namespace, _read_json(source_root / "scripts" / name))
            log = source_root / "knowledge/log.md"
            staged.set_state("build_log", log.read_bytes().decode("utf-8") if log.exists() else "# Build Log\n\n")
            for name in ("index.md", "log.md"):
                path = source_root / "knowledge" / name
                if path.exists():
                    manifest[f"knowledge/{name}"] = content_hash(path.read_bytes().decode("utf-8"))
            usage = staged.get_state("legacy_usage", {}).get("article_reads", {})
            with staged.transaction() as conn:
                for path, data in usage.items():
                    if isinstance(data, dict):
                        conn.execute("INSERT OR REPLACE INTO article_usage VALUES (?,?,?)",
                                     (path, int(data.get("count", 0)), str(data.get("last", ""))))
            recovery_count = import_recovery_contexts(staged, source_root)
            from capture_spool import import_spooled_contexts
            spool_count = len(import_spooled_contexts(staged, source_root, archive=False))
            staged.set_state("exports", {"generation": staged.generation(), "files": manifest})
            staged.set_state("migration", {"source_root": str(source_root), "backup": str(backup), "created": timestamp(),
                                            "articles": len(changes), "sources": source_count, "recovery_contexts": recovery_count,
                                            "spooled_jobs": spool_count})
            errors = staged.integrity_check()
            if errors:
                raise StoreValidationError("Migration integrity check failed: " + "; ".join(errors))
            for article in staged.list_articles():
                if manifest[f"knowledge/{article['path']}.md"] != content_hash(article["body"]):
                    raise StoreConflict("Article content changed during import")
            publish = Path(temporary) / "publish.sqlite"
            staged.backup(publish)
            os.replace(publish, root / "scripts/memory.sqlite")
            # Re-establish WAL at the final location only after publishing a
            # standalone backup. SQLite versions differ on first read-only WAL open.
            MemoryStore(root).initialize()
        if root == source_root:
            # Publication is complete; retain imported spools and catch any
            # hook delivery that arrived while the migration gate was held.
            import_spooled_contexts(MemoryStore(root))
        return {"already_migrated": False, "articles": len(changes), "sources": source_count,
                "recovery_contexts": recovery_count, "spooled_jobs": spool_count, "backup": str(backup)}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parent.parent)
    parser.add_argument("--source-root", type=Path, help="Read a legacy corpus into a different, isolated root")
    args = parser.parse_args()
    print(json.dumps(migrate(args.root, source_root=args.source_root), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
