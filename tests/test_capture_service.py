from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor

import pytest

from capture_service import capture_transcript, spawn_worker
from memory_store import MemoryStore, StoreConflict


def transcript(path, messages, *, claude=False):
    rows = [] if claude else [{"type": "session_meta", "payload": {"id": "session-one", "cwd": "/project"}}]
    for message in messages:
        payload = {"role": "user", "content": message}
        rows.append({"message": payload} if claude else {"type": "response_item", "payload": {"type": "message", **payload}})
    path.write_text("\n".join(json.dumps(row) for row in rows) + "\n")


@pytest.fixture
def store(tmp_path):
    result = MemoryStore(tmp_path)
    result.initialize()
    return result


@pytest.mark.parametrize("claude", [False, True])
def test_capture_is_durable_incremental_and_retains_oversize_message(store, tmp_path, claude):
    path = tmp_path / "session.jsonl"
    text = "BEGIN " + "text " * 12000 + " END"
    transcript(path, [text], claude=claude)
    metadata = {"session_id": "same", "agent": "claude_code" if claude else "codex"}
    ids = capture_transcript(store, path, metadata)
    assert len(ids) > 1
    assert text in "".join(job["context"] for job in store.jobs())
    assert capture_transcript(store, path, metadata) == []
    transcript(path, [text, "new decision"], claude=claude)
    assert len(capture_transcript(store, path, metadata)) == 1
    assert len(store.jobs()) == len(ids) + 1


def test_failed_spawn_retains_queued_capture_and_checkpoint(store, tmp_path, monkeypatch):
    path = tmp_path / "session.jsonl"
    transcript(path, ["important choice"])
    ids = capture_transcript(store, path, {})
    monkeypatch.setattr("capture_service.subprocess.Popen", lambda *a, **kw: (_ for _ in ()).throw(OSError("unavailable")))
    assert not spawn_worker(store, ids)
    assert store.jobs()[0]["status"] == "pending"
    assert capture_transcript(store, path, {}) == []


def test_capture_transaction_rolls_back_checkpoint_on_enqueue_failure(store):
    with pytest.raises(ValueError):
        store.capture_batch("key", expected=None, checkpoint={"message_count": 2}, captures=[
            {"context": "first", "metadata": {}, "identity": "1"},
            {"context": "", "metadata": {}, "identity": "2"},
        ])
    assert store.jobs() == []
    assert store.get_state("capture_checkpoints", {}) == {}


def test_concurrent_capture_does_not_duplicate(store, tmp_path):
    path = tmp_path / "session.jsonl"
    transcript(path, ["one", "two"])
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: capture_transcript(store, path, {}), range(4)))
    assert sum(len(result) for result in results) == 1
    assert len(store.jobs()) == 1


def test_rewritten_transcript_is_not_silently_skipped(store, tmp_path):
    path = tmp_path / "session.jsonl"
    transcript(path, ["original"])
    capture_transcript(store, path, {})
    transcript(path, ["replacement", "later"])
    with pytest.raises(StoreConflict, match="changed"):
        capture_transcript(store, path, {})
    assert len(store.jobs()) == 1


def test_secrets_are_removed_before_queue_persistence(store, tmp_path):
    path = tmp_path / "session.jsonl"
    transcript(path, ["password=super-secret-123 and chosen SQLite"])
    capture_transcript(store, path, {})
    assert "super-secret-123" not in store.jobs()[0]["context"]


def test_explicit_range_import_does_not_checkpoint_uncaptured_messages(store, tmp_path):
    path = tmp_path / "session.jsonl"
    transcript(path, ["first", "second", "third"])
    ids = capture_transcript(store, path, {}, after_message_count=1, until_message_count=2)
    assert len(ids) == 1
    assert "second" in store.jobs()[0]["context"]
    assert "first" not in store.jobs()[0]["context"]
    assert store.get_state("capture_checkpoints", {}) == {}


def test_migrated_recovery_ranges_are_not_queued_twice(store, tmp_path):
    path = tmp_path / "session.jsonl"
    transcript(path, ["first", "second", "third"])
    store.set_state("codex_import", {"session_checkpoints": {"session-one": {"message_count": 2}}})
    store.enqueue("**User:** first\n\n**User:** second", {
        "session_id": "session-one", "agent": "codex", "after": 0, "until": 2,
    }, identity="legacy-range")
    ids = capture_transcript(store, path, {})
    assert len(ids) == 1
    assert store.jobs()[-1]["context"].strip() == "**User:** third"
    assert capture_transcript(store, path, {}) == []


def test_unconfirmed_legacy_reservation_never_discards_context(store, tmp_path):
    path = tmp_path / "session.jsonl"
    transcript(path, ["first", "second"])
    store.set_state("codex_import", {"session_checkpoints": {"session-one": {"message_count": 2}}})
    capture_transcript(store, path, {})
    assert "first" in store.jobs()[0]["context"]
    assert store.jobs()[0]["metadata"]["legacy_unverified"] is True
