"""Reproducible Markdown projections of a consistent SQLite snapshot."""

from __future__ import annotations

import argparse
import os
import tempfile
import uuid
from pathlib import Path

from locking import file_lock
from memory_store import MemoryStore, content_hash


class ExportConflict(RuntimeError):
    """An externally modified export requires explicit import or overwrite."""


def atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        # Never replace an unexpected file created by an external editor after
        # the exporter vacated the destination. Linking publishes atomically.
        try:
            os.link(temporary, path)
        except FileExistsError:
            raise ExportConflict(f"Export destination changed during publication: {path.name}") from None
    finally:
        temporary.unlink(missing_ok=True)


def preserve_previous(path: Path, preserved: Path, allowed: set[str], *, force: bool) -> None:
    """Move the old inode out of the way before publishing; retain every byte."""
    preserved.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.replace(path, preserved)
    except FileNotFoundError:
        return
    changed = preserved.is_symlink() or content_hash(preserved.read_bytes().decode("utf-8")) not in allowed
    if changed and not force:
        try:
            os.link(preserved, path, follow_symlinks=False)
        except FileExistsError:
            pass  # A newer external edit also survives; the moved inode is retained.
        raise ExportConflict(f"Export changed during publication; preserved at {preserved}")


def index_markdown(articles: list[dict]) -> str:
    lines = ["# Knowledge Base Index", "", "| Article | Summary | Compiled From | Updated |", "|---------|---------|---------------|---------|"]
    for article in articles:
        sources = article["sources"]
        cell = ", ".join(sources if len(sources) <= 3 else [sources[0], f"{sources[-1]} +{len(sources)-2} more"])
        lines.append(f"| [[{article['path']}]] | {article['summary']} | {cell} | {article['updated']} |")
    return "\n".join(lines) + "\n"


def snapshot_files(snapshot: dict) -> dict[str, str]:
    files = {f"knowledge/{article['path']}.md": article["body"] for article in snapshot["articles"]}
    files["knowledge/index.md"] = index_markdown(snapshot["articles"])
    files["knowledge/log.md"] = snapshot["build_log"]
    for source in snapshot["sources"]:
        path = source["path"].replace("daily/", "daily/archive/", 1) if source["archived"] else source["path"]
        files[path] = source["content"]
    return files


def export_memory(store: MemoryStore, destination: Path | None = None, *, force: bool = False) -> dict:
    """Publish owned paths atomically; preserve unknown files and changed exports."""
    root = (destination or store.root).resolve()
    canonical = root == store.root
    root.mkdir(parents=True, exist_ok=True)
    with file_lock(root / "scripts/.locks/export.lock"):
        snapshot = store.snapshot()
        files = snapshot_files(snapshot)
        previous = snapshot["export_state"].get("files", {}) if canonical else {}
        pending = store.get_state("export_pending", {}).get("files", {}) if canonical else {}
        hashes = {path: content_hash(body) for path, body in files.items()}
        trusted = {path: set(pending.get(path, [])) | {h for h in (previous.get(path), hashes.get(path)) if h}
                   for path in set(files) | set(previous) | set(pending)}
        conflicts = []
        for relative in set(files) | set(previous) | set(pending):
            path = root / relative
            if path.is_symlink() or not path.resolve().is_relative_to(root):
                raise ExportConflict(f"Export path follows a symlink: {relative}")
            if not path.exists():
                continue
            if not path.is_file():
                raise ExportConflict(f"Export path is not a file: {relative}")
            actual = content_hash(path.read_bytes().decode("utf-8"))
            if actual not in trusted[relative]:
                conflicts.append(relative)
        if conflicts and not force:
            raise ExportConflict("Externally modified exports: " + ", ".join(sorted(conflicts)))
        if canonical:
            # Survives a crash before the completed manifest is installed. Keep
            # prior interrupted targets trusted even if canonical content changes.
            store.set_state("export_pending", {"generation": snapshot["generation"],
                                               "files": {path: sorted(values) for path, values in trusted.items()}})
        run = str(uuid.uuid4())
        written = 0
        for relative, body in files.items():
            path = root / relative
            if path.exists() and path.read_bytes() == body.encode("utf-8"):
                continue
            category = "export-conflicts" if relative in conflicts else "export-revisions"
            preserved = root / "reports" / category / str(snapshot["generation"]) / run / relative
            preserve_previous(path, preserved, trusted[relative], force=force)
            atomic_write(path, body)
            written += 1
        retired = 0
        for relative in (set(previous) | set(pending)) - files.keys():
            path = root / relative
            if path.exists():
                preserved = root / "reports/retired-exports" / str(snapshot["generation"]) / run / relative
                preserve_previous(path, preserved, trusted[relative], force=force)
                retired += 1
        if canonical:
            store.set_state("exports", {"generation": snapshot["generation"], "files": hashes})
            store.set_state("export_pending", {})
        return {"articles": len(snapshot["articles"]), "sources": len(snapshot["sources"]), "written": written, "retired": retired}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parent.parent)
    parser.add_argument("--destination", type=Path)
    parser.add_argument("--force", action="store_true", help="Preserve changed files in reports/export-conflicts before overwriting")
    args = parser.parse_args()
    print(export_memory(MemoryStore(args.root), args.destination, force=args.force))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
