from __future__ import annotations

import builtins
import errno
import importlib.util
import io
import json
import sqlite3
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier, Event
from types import SimpleNamespace

import pytest

import capture_spool
from locking import file_lock
from memory_store import MemoryStore, StoreConflict
from memory_export import export_memory


@pytest.fixture(autouse=True)
def no_external_work(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("Spool tests must mock every process launch")
    monkeypatch.setattr(capture_spool.subprocess, "Popen", forbidden)
    monkeypatch.setattr(capture_spool, "spawn_worker", forbidden)


def load_hook(name: str, root: Path, monkeypatch):
    monkeypatch.delenv("CLAUDE_INVOKED_BY", raising=False)
    monkeypatch.delenv("MEMORY_COMPILER_INTERNAL", raising=False)
    path = Path(__file__).resolve().parents[1] / "hooks" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name.replace("-", "_"), path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "ROOT", root)
    monkeypatch.setattr(module, "SCRIPTS_DIR", root / "scripts")
    return module


def transcript(path: Path, *, codex: bool) -> str:
    text = "Full beginning password=super-secret-value " + "important knowledge " * 3000 + " COMPLETE END"
    if codex:
        rows = [
            {"type": "session_meta", "payload": {"id": "s1", "cwd": "/project", "model_provider": "openai"}},
            {"type": "response_item", "payload": {"type": "message", "role": "user", "content": text}},
        ]
    else:
        rows = [{"message": {"role": "user", "content": text}}]
    path.write_text("\n".join(json.dumps(row) for row in rows) + "\n")
    return text


@pytest.mark.parametrize("name", ["codex-stop", "pre-compact", "session-end"])
def test_blocked_hook_returns_promptly_after_complete_sanitized_spool(tmp_path, monkeypatch, name) -> None:
    module = load_hook(name, tmp_path, monkeypatch)
    path = tmp_path / "session.jsonl"
    transcript(path, codex=name == "codex-stop")
    payload = {"transcript_path": str(path), "session_id": "s1", "cwd": "/project", "secret": "NEVER PERSIST RAW PAYLOAD"}
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))
    spawned = []
    def spawn(command, **kwargs):
        files = list((tmp_path / "reports/capture-spool").glob("*.json"))
        assert len(files) == 1  # Durability comes before process launch.
        spawned.append((command, kwargs))
        return SimpleNamespace(pid=1234)
    monkeypatch.setattr(capture_spool.subprocess, "Popen", spawn)
    held, release = Event(), Event()
    def hold_migration():
        with file_lock(tmp_path / "scripts/.locks/migration.lock"):
            held.set()
            assert release.wait(3)
    with ThreadPoolExecutor(max_workers=1) as pool:
        holder = pool.submit(hold_migration)
        assert held.wait(1)
        started = time.monotonic()
        try:
            assert module.main() == 0
            assert time.monotonic() - started < 1
        finally:
            release.set()
        holder.result(timeout=2)
    files = list((tmp_path / "reports/capture-spool").glob("*.json"))
    serialized = files[0].read_text()
    entry = json.loads(serialized)
    assert "Full beginning" in entry["context"]
    assert "COMPLETE END" in entry["context"]
    assert len(entry["context"]) > 50000
    assert "super-secret-value" not in serialized
    assert "NEVER PERSIST RAW PAYLOAD" not in serialized
    assert entry["metadata"]["session_id"] == "s1"
    assert entry["metadata"]["cwd"] == "/project"
    assert spawned[0][1]["start_new_session"] is True
    assert spawned[0][1]["stdin"] == capture_spool.subprocess.DEVNULL
    assert not MemoryStore(tmp_path).db_path.exists()


def test_spawn_failure_retains_spool_and_records_only_failure_category(tmp_path, monkeypatch) -> None:
    path = tmp_path / "session.jsonl"
    transcript(path, codex=True)
    def fail(*args, **kwargs):
        raise OSError("credentials password=do-not-log-this")
    monkeypatch.setattr(capture_spool.subprocess, "Popen", fail)
    status = capture_spool._spool_hook(tmp_path, json.dumps({"transcript_path": str(path)}), agent="codex", source="hook:stop")
    assert status == 1
    assert len(list((tmp_path / "reports/capture-spool").glob("*.json"))) == 1
    events = list((tmp_path / "reports/capture-spool/failures").glob("*.json"))
    assert json.loads(events[0].read_text())["reason"] == "importer_spawn_failed"
    assert "do-not-log-this" not in events[0].read_text()


def test_import_is_idempotent_bounded_and_never_advances_checkpoints(tmp_path) -> None:
    store = MemoryStore(tmp_path)
    store.initialize()
    content = "Beginning " + "complete source " * 3000 + "end"
    metadata = {"session_id": "s1", "agent": "codex", "transcript_path": "/session.jsonl", "captured_at": "2026-09-05T12:00:00+05:00"}
    path = capture_spool.persist_spooled_context(tmp_path, content, metadata)
    first = capture_spool.import_spooled_contexts(store)
    assert len(first) > 1
    assert "".join(job["context"] for job in store.jobs()) == content
    assert all(len(job["context"]) <= capture_spool.MAX_JOB_CHARS for job in store.jobs())
    assert store.get_state("capture_checkpoints", {}) == {}
    assert not path.exists()
    assert (path.parent / "imported" / path.name).is_file()
    capture_spool.persist_spooled_context(tmp_path, content, {**metadata, "captured_at": "2026-09-05T12:01:00+05:00"})
    assert capture_spool.import_spooled_contexts(store) == first
    assert len(store.jobs()) == len(first)
    assert store.get_state("capture_checkpoints", {}) == {}


def test_staged_import_leaves_original_spool_untouched(tmp_path) -> None:
    source = tmp_path / "original"
    path = capture_spool.persist_spooled_context(source, "Durable source", {"session_id": "s1"})
    before = path.read_bytes()
    store = MemoryStore(tmp_path / "staged")
    store.initialize()
    first = capture_spool.import_spooled_contexts(store, source_root=source, archive=False)
    assert first == capture_spool.import_spooled_contexts(store, source_root=source, archive=False)
    assert path.read_bytes() == before
    assert not (path.parent / "imported").exists()
    with pytest.raises(ValueError, match="archive=False"):
        capture_spool.import_spooled_contexts(store, source_root=source)


def test_invalid_or_modified_spool_is_retained(tmp_path) -> None:
    store = MemoryStore(tmp_path)
    store.initialize()
    path = capture_spool.persist_spooled_context(tmp_path, "Original", {"session_id": "s1"})
    payload = json.loads(path.read_text())
    payload["context"] = "Changed after publication"
    path.write_text(json.dumps(payload))
    assert capture_spool.import_spooled_contexts(store) == []
    assert path.exists()
    assert store.jobs() == []


def test_importer_waits_outside_hook_and_retains_spool_without_database(tmp_path) -> None:
    path = capture_spool.persist_spooled_context(tmp_path, "Durable source", {"session_id": "s1"})
    assert capture_spool.main(["--root", str(tmp_path), "--no-flush"]) == 0
    assert path.exists()


def test_concurrent_identical_capture_keeps_first_complete_spool(tmp_path, monkeypatch) -> None:
    rendezvous = Barrier(2)
    original = capture_spool.atomic_write
    def concurrent_write(path, content):
        rendezvous.wait(timeout=2)
        original(path, content)
    monkeypatch.setattr(capture_spool, "atomic_write", concurrent_write)
    def persist(index: int):
        return capture_spool.persist_spooled_context(tmp_path, "Same complete context", {
            "session_id": "s1", "captured_at": f"2026-09-05T12:00:0{index}+05:00",
        })
    with ThreadPoolExecutor(max_workers=2) as pool:
        paths = list(pool.map(persist, range(2)))
    assert paths[0] == paths[1]
    assert json.loads(paths[0].read_text())["context"] == "Same complete context"
    assert len(list(paths[0].parent.glob("*.json"))) == 1


def test_importer_rechecks_canonical_after_waiting_for_publication(tmp_path) -> None:
    capture_spool.persist_spooled_context(tmp_path, "Durable source", {"session_id": "s1"})
    entered = Event()
    def importer():
        entered.set()
        return capture_spool.main(["--root", str(tmp_path), "--no-flush"])
    with ThreadPoolExecutor(max_workers=1) as pool:
        with file_lock(tmp_path / "scripts/.locks/migration.lock"):
            future = pool.submit(importer)
            assert entered.wait(1)
            assert not future.done()
            MemoryStore(tmp_path).initialize()
        assert future.result(timeout=2) == 0
    assert MemoryStore(tmp_path).jobs()[0]["context"] == "Durable source"


def test_missing_transcript_event_never_persists_raw_payload(tmp_path) -> None:
    assert capture_spool._spool_hook(tmp_path, '{"password":"RAW SECRET"}', agent="codex", source="hook:stop") == 1
    event = next((tmp_path / "reports/capture-spool/failures").glob("*.json")).read_text()
    assert "missing_transcript_path" in event
    assert "RAW SECRET" not in event


def test_available_hook_gate_preserves_existing_hook_behavior(tmp_path) -> None:
    observed = []
    @capture_spool.guard_capture_hook(lambda: tmp_path, agent="codex", source="hook:stop")
    def normal_hook():
        with capture_spool.try_file_lock(tmp_path / "scripts/.locks/migration.lock") as locked:
            observed.append(locked)
        return "normal"
    assert normal_hook() == "normal"
    assert observed == [False]


@pytest.mark.parametrize("name", ["codex-stop", "pre-compact", "session-end"])
@pytest.mark.parametrize("path_kind", ["missing", "directory", "empty"])
def test_unlocked_hook_reports_unavailable_transcript(tmp_path, monkeypatch, name, path_kind):
    import health

    store = MemoryStore(tmp_path)
    store.initialize()
    export_memory(store)
    module = load_hook(name, tmp_path, monkeypatch)
    path = tmp_path / "private-missing-session.jsonl"
    if path_kind == "directory":
        path.mkdir()
    payload = {"transcript_path": "" if path_kind == "empty" else str(path),
               "session_id": "private-session", "secret": "RAW SECRET"}
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))
    if name == "codex-stop":
        # A supplied but unavailable transcript must not import another session.
        def unrelated_session():
            raise AssertionError("Explicit transcript must not fall back to scanning")
        monkeypatch.setattr(module, "resolve_legacy_transcript", unrelated_session)
    assert module.main() == 1
    events = list((tmp_path / "reports/capture-spool/failures").glob("*.json"))
    assert len(events) == 1
    serialized = events[0].read_text()
    assert "RAW SECRET" not in serialized
    assert "private-session" not in serialized
    assert "private-missing-session" not in serialized
    assert store.jobs() == []
    assert store.get_state("capture_checkpoints", {}) == {}
    monkeypatch.setattr(health, "KNOWLEDGE_DIR", tmp_path / "knowledge")
    monkeypatch.setattr(health, "load_runtime_config", lambda: {})
    report = health.collect_health()
    assert report.status == "attention"
    assert report.export_drift == []
    assert report.capture_spool_files == [str(events[0].relative_to(tmp_path))]


@pytest.mark.parametrize("error", [OSError, sqlite3.OperationalError, ValueError, StoreConflict])
def test_unlocked_hook_records_safe_failure_without_database(tmp_path, error):
    @capture_spool.guard_capture_hook(lambda: tmp_path, agent="claude_code", source="session-end")
    def broken_hook():
        raise error("RAW SECRET from failed capture")

    assert broken_hook() == 1
    event = next((tmp_path / "reports/capture-spool/failures").glob("*.json")).read_text()
    assert error.__name__ in event
    assert "RAW SECRET" not in event


def test_unlocked_hook_does_not_swallow_cancellation(tmp_path):
    @capture_spool.guard_capture_hook(lambda: tmp_path, agent="claude_code", source="session-end")
    def cancelled_hook():
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        cancelled_hook()
    assert not (tmp_path / "reports/capture-spool/failures").exists()


@pytest.mark.parametrize("name", ["codex-stop", "pre-compact", "session-end"])
def test_unlocked_hook_still_captures_complete_valid_transcript(tmp_path, monkeypatch, name):
    import capture_service

    store = MemoryStore(tmp_path)
    store.initialize()
    module = load_hook(name, tmp_path, monkeypatch)
    path = tmp_path / "session.jsonl"
    transcript(path, codex=name == "codex-stop")
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps({"transcript_path": str(path)})))
    monkeypatch.setattr(capture_service, "spawn_worker", lambda store, ids: True)
    assert module.main() in (None, 0)
    context = "".join(job["context"] for job in store.jobs())
    assert "Full beginning" in context and "COMPLETE END" in context
    assert "super-secret-value" not in context
    assert len(store.get_state("capture_checkpoints")) == 1
    assert not (tmp_path / "reports/capture-spool/failures").exists()


def test_codex_hook_without_transcript_field_retains_legacy_discovery(tmp_path, monkeypatch):
    import capture_service

    store = MemoryStore(tmp_path)
    store.initialize()
    module = load_hook("codex-stop", tmp_path, monkeypatch)
    transcript(tmp_path / "rollout-fixture.jsonl", codex=True)
    monkeypatch.setattr(module, "CODEX_SESSIONS_DIR", tmp_path)
    monkeypatch.setattr(sys, "stdin", io.StringIO("{}"))
    monkeypatch.setattr(capture_service, "spawn_worker", lambda store, ids: True)
    assert module.main() in (None, 0)
    assert store.jobs()[0]["metadata"]["session_id"] == "s1"
    assert "COMPLETE END" in "".join(job["context"] for job in store.jobs())


def test_unlocked_hook_returns_failure_when_marker_cannot_be_written(tmp_path, monkeypatch, capsys):
    @capture_spool.guard_capture_hook(lambda: tmp_path, agent="claude_code", source="session-end")
    def broken_hook():
        raise OSError("RAW SECRET")

    def unavailable(*args, **kwargs):
        raise OSError("RAW SECRET from filesystem")

    monkeypatch.setattr(capture_spool, "atomic_write", unavailable)
    assert broken_hook() == 1
    assert "could not be persisted" in capsys.readouterr().err


def test_windows_nonblocking_lock_uses_immediate_mode_and_never_unlocks_failed_acquisition(tmp_path, monkeypatch) -> None:
    original_import = builtins.__import__
    calls = []
    fake = SimpleNamespace(LK_NBLCK=2, LK_UNLCK=0)
    def locking(descriptor, mode, size):
        calls.append(mode)
        raise OSError(errno.EACCES, "busy")
    fake.locking = locking
    def import_without_fcntl(name, *args, **kwargs):
        if name == "fcntl":
            raise ImportError("Windows")
        return original_import(name, *args, **kwargs)
    monkeypatch.setattr(builtins, "__import__", import_without_fcntl)
    monkeypatch.setitem(sys.modules, "msvcrt", fake)
    with capture_spool.try_file_lock(tmp_path / "gate.lock") as acquired:
        assert acquired is False
    assert calls == [fake.LK_NBLCK]
