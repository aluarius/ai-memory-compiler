from __future__ import annotations

import json
from pathlib import Path

import pytest

from memory_export import ExportConflict, export_memory
from memory_migrate import migrate
from memory_store import MemoryStore


BODY = '''---
title: "Example"
sources:
  - daily/archive/2026-09-01.md
created: 2026-09-01
updated: 2026-09-01
---

# Example

Exact text — сохраняем пробелы.\x20\x20

## Sources
- [[daily/archive/2026-09-01]]
'''


def legacy(root: Path) -> None:
    (root / "knowledge/concepts").mkdir(parents=True)
    (root / "daily/archive").mkdir(parents=True)
    (root / "scripts").mkdir()
    (root / "knowledge/concepts/example.md").write_text(BODY)
    (root / "daily/archive/2026-09-01.md").write_text("# Source\nExact original.\n")
    (root / "knowledge/index.md").write_text(
        "# Index\n\n| [[concepts/example]] | Original summary | daily/archive/2026-09-01.md | 2026-09-01 |\n"
    )
    (root / "knowledge/log.md").write_text("# Build Log\n\nHistorical log.\n")
    (root / "scripts/state.json").write_text(json.dumps({"ingested": {"2026-09-01.md": {"hash": "legacy"}}}))
    failed = root / "reports/failed-flushes"
    failed.mkdir(parents=True)
    (failed / "import-flush-019fd1a2-ea5e-7911-893c-a952b63155fa-3-7-20260905-120000.md").write_text("Sanitized capture")


def test_migration_preserves_originals_and_is_idempotent(tmp_path: Path) -> None:
    legacy(tmp_path)
    before = (tmp_path / "knowledge/concepts/example.md").read_bytes()
    result = migrate(tmp_path)
    store = MemoryStore(tmp_path)
    assert result["articles"] == 1
    assert Path(result["backup"]).is_file()
    assert store.read_article("concepts/example")["body"].encode() == before
    assert store.read_article("concepts/example")["summary"] == "Original summary"
    assert store.get_state("pipeline")["ingested"]["2026-09-01.md"]["hash"] == "legacy"
    assert store.get_state("build_log") == "# Build Log\n\nHistorical log.\n"
    assert len(store.jobs()) == 1
    assert store.jobs()[0]["metadata"]["after"] == 3
    assert store.jobs()[0]["metadata"]["until"] == 7
    assert (tmp_path / "knowledge/concepts/example.md").read_bytes() == before
    assert migrate(tmp_path)["already_migrated"] is True
    assert len(store.jobs()) == 1


def test_failed_migration_does_not_publish_partial_database(tmp_path: Path) -> None:
    legacy(tmp_path)
    path = tmp_path / "knowledge/concepts/example.md"
    path.write_text(BODY + "\n[[concepts/missing]]\n")
    with pytest.raises(ValueError, match="missing"):
        migrate(tmp_path)
    assert not MemoryStore.is_initialized(tmp_path)
    assert path.read_text().endswith("[[concepts/missing]]\n")


def test_export_roundtrip_is_exact_and_conflicts_are_preserved(tmp_path: Path) -> None:
    root = tmp_path / "legacy"
    legacy(root)
    migrate(root)
    store = MemoryStore(root)
    destination = tmp_path / "export"
    export_memory(store, destination=destination)
    assert (destination / "knowledge/concepts/example.md").read_text() == BODY
    assert (destination / "daily/archive/2026-09-01.md").read_text() == "# Source\nExact original.\n"
    export_memory(store)
    path = root / "knowledge/concepts/example.md"
    path.write_text("User modification")
    with pytest.raises(ExportConflict):
        export_memory(store)
    assert path.read_text() == "User modification"
    export_memory(store, force=True)
    assert path.read_text() == BODY


def test_export_does_not_delete_unrelated_files(tmp_path: Path) -> None:
    legacy(tmp_path)
    migrate(tmp_path)
    extra = tmp_path / "knowledge/handwritten.txt"
    extra.write_text("outside export")
    export_memory(MemoryStore(tmp_path))
    assert extra.read_text() == "outside export"


def test_crlf_bytes_survive_import_and_export(tmp_path):
    legacy(tmp_path)
    article = tmp_path / "knowledge/concepts/example.md"
    source = tmp_path / "daily/archive/2026-09-01.md"
    article.write_bytes(BODY.replace("\n", "\r\n").encode())
    source.write_bytes(b"# Source\r\nExact text.\r\n")
    before = {path.relative_to(tmp_path): path.read_bytes() for path in (article, source)}
    migrate(tmp_path)
    destination = tmp_path / "roundtrip"
    export_memory(MemoryStore(tmp_path), destination)
    assert {path: (destination / path).read_bytes() for path in before} == before


def test_export_crash_then_new_commit_can_resume(tmp_path, monkeypatch):
    import memory_export
    legacy(tmp_path)
    migrate(tmp_path)
    store = MemoryStore(tmp_path)
    export_memory(store)
    change = store.read_article("concepts/example")
    change["body"] += "\nVersion B\n"
    store.commit_articles([change])
    original = memory_export.atomic_write
    calls = 0

    def crash_after_write(path, body):
        nonlocal calls
        original(path, body)
        calls += 1
        if calls == 1:
            raise OSError("simulated process failure")

    monkeypatch.setattr(memory_export, "atomic_write", crash_after_write)
    with pytest.raises(OSError):
        export_memory(store)
    change["body"] += "Version C\n"
    store.commit_articles([change])
    monkeypatch.setattr(memory_export, "atomic_write", original)
    export_memory(store)
    assert (tmp_path / "knowledge/concepts/example.md").read_text() == change["body"]


def test_export_preserves_edit_after_preflight(tmp_path, monkeypatch):
    legacy(tmp_path)
    migrate(tmp_path)
    store = MemoryStore(tmp_path)
    export_memory(store)
    change = store.read_article("concepts/example")
    change["body"] += "\nNext version\n"
    store.commit_articles([change])
    path = tmp_path / "knowledge/concepts/example.md"
    original = store.set_state

    def concurrent_edit(namespace, value):
        original(namespace, value)
        if namespace == "export_pending":
            path.write_text("Concurrent human edit")

    monkeypatch.setattr(store, "set_state", concurrent_edit)
    with pytest.raises(ExportConflict):
        export_memory(store)
    preserved = list((tmp_path / "reports").rglob("example.md"))
    assert path.read_text() == "Concurrent human edit" or any(p.read_text() == "Concurrent human edit" for p in preserved)


def test_pending_only_export_is_retired_after_canonical_deletion(tmp_path, monkeypatch):
    import memory_export
    legacy(tmp_path)
    migrate(tmp_path)
    store = MemoryStore(tmp_path)
    export_memory(store)
    change = store.read_article("concepts/example")
    change["path"] = "concepts/new"
    store.commit_articles([change])
    original = memory_export.atomic_write
    def crash(path, body):
        original(path, body)
        raise OSError("crash")
    monkeypatch.setattr(memory_export, "atomic_write", crash)
    with pytest.raises(OSError):
        export_memory(store)
    assert (tmp_path / "knowledge/concepts/new.md").is_file()
    store.commit_articles([], deletions=["concepts/new"])
    monkeypatch.setattr(memory_export, "atomic_write", original)
    export_memory(store)
    assert not (tmp_path / "knowledge/concepts/new.md").exists()
    assert list((tmp_path / "reports/retired-exports").rglob("new.md"))
