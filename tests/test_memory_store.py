from __future__ import annotations

from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing, contextmanager
import sqlite3
from pathlib import Path

import pytest

from memory_store import MemoryStore, StoreConflict, StoreValidationError


def article(name: str = "topic", links: str = "") -> dict:
    return {
        "path": f"concepts/{name}",
        "summary": f"Summary of {name}",
        "body": f'''---
title: "{name}"
sources:
  - daily/2026-09-01.md
created: 2026-09-01
updated: 2026-09-01
---

# {name}

Durable knowledge. {links}

## Sources

- [[daily/2026-09-01]]
''',
        "projects": ["example"],
    }


@pytest.fixture
def store(tmp_path: Path) -> MemoryStore:
    result = MemoryStore(tmp_path)
    result.initialize()
    result.import_source("daily/2026-09-01.md", "# Source\n", archived=False)
    return result


def test_articles_revisions_and_sources_survive_without_exports(store: MemoryStore) -> None:
    first = article()
    store.commit_articles([first])
    initial = store.read_article("concepts/topic")
    assert initial["body"] == first["body"]
    assert initial["revision"] == 1
    first["body"] += "\nAnother fact.\n"
    store.commit_articles([first])
    assert store.read_article("concepts/topic.md")["revision"] == 2
    assert store.revisions("concepts/topic")[0]["body"] == initial["body"]
    assert store.read_source("daily/archive/2026-09-01.md") == "# Source\n"
    assert store.list_articles(project="example")[0]["path"] == "concepts/topic"
    assert store.list_articles(project="unrelated") == []


@pytest.mark.parametrize("duration", [0, -1, float('inf'), float('nan')])
def test_lease_renewal_rejects_invalid_duration(store, duration):
    job_id = store.enqueue('context', {}, identity='renewal')
    lease = store.claim_job(job_id)
    with pytest.raises(ValueError, match='positive'):
        store.renew_job_lease(job_id, lease['lease_token'], lease_seconds=duration)
    assert store.jobs()[0]['lease_until'] == lease['lease_until']


def test_lease_renewal_cannot_reopen_completed_job(store):
    job_id = store.enqueue('context', {}, identity='renewal')
    lease = store.claim_job(job_id)
    store.complete_job(job_id, lease['lease_token'], '', 'daily/2026-09-01.md')
    with pytest.raises(StoreConflict):
        store.renew_job_lease(job_id, lease['lease_token'])
    assert store.jobs()[0]['status'] == 'done'


def test_commit_rejects_broken_links_and_preserves_checkpoint(store: MemoryStore) -> None:
    store.commit_articles([article()])
    generation = store.generation()
    invalid = article("new", "[[concepts/missing]]")
    with pytest.raises(StoreValidationError, match="missing"):
        store.commit_articles(
            [invalid], source_updates={"2026-09-01.md": {"hash": "new"}},
            build_entry="Failed change",
        )
    assert store.read_article("concepts/new") is None
    assert store.generation() == generation
    assert "2026-09-01.md" not in store.get_state("pipeline", {}).get("ingested", {})
    assert store.get_state("build_log", "") == ""


def test_commit_accepts_mutual_new_links_in_one_transaction(store: MemoryStore) -> None:
    store.commit_articles([article("a", "[[concepts/b]]"), article("b", "[[concepts/a]]")])
    assert len(store.list_articles()) == 2


def test_daily_links_outside_frontmatter_must_exist(store):
    with pytest.raises(StoreValidationError, match="missing"):
        store.commit_articles([article(links="[[daily/2026-09-02]]")])
    assert store.generation() == 0


def test_backup_is_self_contained_and_readonly_after_relocation(store, tmp_path):
    store.commit_articles([article()])
    backup = tmp_path / "snapshot.sqlite"
    store.backup(backup)
    relocated = tmp_path / "relocated.sqlite"
    backup.rename(relocated)
    # A portable snapshot uses rollback-journal headers, not missing WAL sidecars.
    assert relocated.read_bytes()[18:20] == b"\x01\x01"
    with sqlite3.connect(relocated.as_uri() + "?mode=ro", uri=True) as connection:
        assert connection.execute("SELECT count(*) FROM articles").fetchone()[0] == 1


@pytest.mark.parametrize("occupied", ["file", "directory", "dangling-symlink"])
def test_backup_preserves_an_occupied_destination(
    store: MemoryStore, tmp_path: Path, occupied: str,
) -> None:
    destination = tmp_path / "backup.sqlite"
    missing_target = tmp_path / "missing.sqlite"
    if occupied == "file":
        destination.write_bytes(b"Existing backup")
    elif occupied == "directory":
        destination.mkdir()
    else:
        destination.symlink_to(missing_target)

    with pytest.raises(FileExistsError):
        store.backup(destination)

    if occupied == "file":
        assert destination.read_bytes() == b"Existing backup"
    elif occupied == "directory":
        assert destination.is_dir()
    else:
        assert destination.is_symlink()
        assert not missing_target.exists()


def test_backup_preserves_destination_created_during_snapshot(
    store: MemoryStore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    backup_directory = tmp_path / "backups"
    backup_directory.mkdir()
    destination = backup_directory / "snapshot.sqlite"
    original_connect = store.connect
    competing_bytes = None

    @contextmanager
    def create_competing_backup(readonly: bool = False) -> Iterator[sqlite3.Connection]:
        nonlocal competing_bytes
        with closing(sqlite3.connect(destination)) as competing, competing:
            competing.execute("CREATE TABLE older_snapshot(value TEXT)")
            competing.execute("INSERT INTO older_snapshot VALUES ('Retain this backup')")
        competing_bytes = destination.read_bytes()
        with original_connect(readonly=readonly) as connection:
            yield connection

    monkeypatch.setattr(store, "connect", create_competing_backup)
    with pytest.raises(FileExistsError):
        store.backup(destination)

    assert destination.read_bytes() == competing_bytes
    assert list(backup_directory.iterdir()) == [destination]


def test_failed_backup_does_not_publish_a_partial_destination(
    store: MemoryStore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    backup_directory = tmp_path / "backups"
    backup_directory.mkdir()
    destination = backup_directory / "snapshot.sqlite"

    class InterruptedSnapshot:
        def backup(self, target: sqlite3.Connection) -> None:
            target.execute("CREATE TABLE partial_snapshot(value TEXT)")
            raise OSError("Snapshot interrupted")

    @contextmanager
    def interrupted_connect(readonly: bool = False) -> Iterator[InterruptedSnapshot]:
        yield InterruptedSnapshot()

    monkeypatch.setattr(store, "connect", interrupted_connect)
    with pytest.raises(OSError, match="Snapshot interrupted"):
        store.backup(destination)

    assert not destination.exists()
    assert list(backup_directory.iterdir()) == []


def test_stale_generation_cannot_publish_results(store: MemoryStore) -> None:
    old = store.generation()
    store.commit_articles([article()])
    with pytest.raises(StoreConflict):
        store.commit_articles([article("other")], expected_generation=old)
    assert store.read_article("concepts/other") is None


@pytest.mark.parametrize("path", ["../secret", "/tmp/x", "concepts/../../x", "concepts/a/b", "concepts\\x"])
def test_article_path_cannot_escape_store(store: MemoryStore, path: str) -> None:
    change = article()
    change["path"] = path
    with pytest.raises(StoreValidationError):
        store.commit_articles([change])


def test_sources_are_append_only_and_idempotent(store: MemoryStore) -> None:
    store.append_source("daily/2026-09-01.md", "First\n", event_key="event-1")
    store.append_source("daily/2026-09-01.md", "First\n", event_key="event-1")
    assert store.read_source("daily/2026-09-01") == "# Source\nFirst\n"
    with pytest.raises(StoreConflict):
        store.import_source("daily/2026-09-01.md", "different", archived=False)


def test_job_completion_and_daily_append_are_idempotent(store: MemoryStore) -> None:
    metadata = {"session_id": "s1", "agent": "codex", "cwd": "/repo/example", "after": 3, "until": 7}
    job_id = store.enqueue("conversation", metadata, identity="s1:3:7")
    assert store.enqueue("conversation", metadata, identity="s1:3:7") == job_id
    job = store.claim_job(job_id)
    assert job is not None
    assert store.claim_job(job_id) is None
    store.complete_job(job_id, job["lease_token"], "Saved\n", "daily/2026-09-01.md")
    assert store.claim_job(job_id, force=True) is None
    assert store.read_source("daily/2026-09-01") == "# Source\nSaved\n"
    with pytest.raises(StoreConflict):
        store.complete_job(job_id, job["lease_token"], "Saved again", "daily/2026-09-01.md")


def test_only_one_concurrent_worker_can_claim_a_job(store: MemoryStore) -> None:
    job_id = store.enqueue("context", {"session_id": "s1"}, identity="s1")
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: store.claim_job(job_id), range(4)))
    assert sum(result is not None for result in results) == 1


def test_retry_keeps_context_and_rejects_expired_worker(store: MemoryStore) -> None:
    job_id = store.enqueue("context", {"session_id": "s1"}, identity="s1")
    first = store.claim_job(job_id)
    store.fail_job(job_id, first["lease_token"], "provider unavailable", delay_seconds=0)
    second = store.claim_job(job_id)
    assert second["context"] == "context"
    assert second["attempts"] == 2
    assert second["lease_token"] != first["lease_token"]
    with pytest.raises(StoreConflict):
        store.complete_job(job_id, first["lease_token"], "old result", "daily/2026-09-01.md")


def test_deletion_cannot_leave_dangling_links(store: MemoryStore) -> None:
    store.commit_articles([article("a", "[[concepts/b]]"), article("b")])
    with pytest.raises(StoreValidationError):
        store.commit_articles([], deletions=["concepts/b"])
    store.commit_articles([article("a")], deletions=["concepts/b"])
    assert store.read_article("concepts/b") is None
    assert store.revisions("concepts/b")[-1]["deleted"] is True


def test_namespaced_state_updates_do_not_lose_concurrent_writes(store: MemoryStore) -> None:
    def increment(_: int) -> None:
        def change(state: dict) -> None:
            state["count"] = state.get("count", 0) + 1
        store.update_state("usage", change)
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(increment, range(30)))
    assert store.get_state("usage")["count"] == 30
