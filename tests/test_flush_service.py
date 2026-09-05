from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

import flush_service
from memory_export import ExportConflict
from memory_store import MemoryStore, StoreConflict
from model_runtime import ModelResult

SAVED = "**Context:** SQLite memory migration\n\n**Lessons Learned:**\n- Acquire the runtime lock before claiming a job."


@pytest.fixture
def store(tmp_path: Path, monkeypatch) -> MemoryStore:
    result = MemoryStore(tmp_path)
    result.initialize()
    async def forbidden(*args, **kwargs) -> ModelResult:
        raise AssertionError("Tests must explicitly stub every model call")
    monkeypatch.setattr(flush_service, "call_readonly_model", forbidden)
    def forbidden_spawn(*args, **kwargs):
        raise AssertionError("Tests must explicitly stub every subprocess launch")
    monkeypatch.setattr(flush_service.subprocess, "Popen", forbidden_spawn)
    return result


def stub_model(monkeypatch, response: str = SAVED) -> list[dict]:
    calls = []
    async def call(prompt: str, **kwargs) -> ModelResult:
        calls.append({"prompt": prompt, **kwargs})
        return ModelResult(text=response)
    monkeypatch.setattr(flush_service, "call_readonly_model", call)
    return calls


def enqueue(store: MemoryStore, identity: str = "s1", **metadata) -> str:
    return store.enqueue("Conversation context", {
        "session_id": identity, "agent": "codex", "provider": "openai",
        "captured_at": "2026-08-30T23:42:00+05:00", **metadata,
    }, identity=identity)


def test_worker_commits_original_date_once_and_recovers_without_context_file(store, monkeypatch) -> None:
    job_id = enqueue(store)
    calls = stub_model(monkeypatch)
    assert flush_service.process_jobs(store, job_id=job_id) == 0
    source = store.read_source("daily/2026-08-30.md")
    assert source.count(SAVED) == 1
    assert "session=s1" in source
    assert "23:42" in source
    assert store.jobs()[0]["status"] == "done"
    assert (store.root / "daily/2026-08-30.md").read_text() == source
    assert flush_service.process_jobs(store, job_id=job_id) == 0
    assert len(calls) == 1
    assert store.read_source("daily/2026-08-30.md") == source


def test_runtime_lock_is_acquired_before_claim_and_covers_model(store, monkeypatch) -> None:
    job_id = enqueue(store)
    held = []
    original_lock = flush_service.file_lock
    @contextmanager
    def lock(path: Path):
        assert path == store.root / "scripts/.locks/flush-llm.lock"
        with original_lock(path):
            held.append(path)
            try:
                yield
            finally:
                held.pop()
    monkeypatch.setattr(flush_service, "file_lock", lock)
    original_claim = store.claim_job
    def claim(*args, **kwargs):
        assert held
        assert kwargs["lease_seconds"] == 1500
        return original_claim(*args, **kwargs)
    monkeypatch.setattr(store, "claim_job", claim)
    async def call(prompt: str, **kwargs) -> ModelResult:
        assert held
        assert kwargs["timeout_seconds"] == 1200
        assert kwargs["task"] == "flush"
        assert not kwargs["cwd"].is_relative_to(store.root)
        return ModelResult(text=SAVED)
    monkeypatch.setattr(flush_service, "call_readonly_model", call)
    assert flush_service.process_jobs(store, job_id=job_id) == 0


@pytest.mark.parametrize("response", ["", " ", "prose without structure", "**Context:**", "**Lessons Learned:**\n- ", "FLUSH_ERROR: unavailable", "FLUSH_OK\nmore text"])
def test_malformed_output_retains_failed_job_and_never_creates_source(store, monkeypatch, response) -> None:
    job_id = enqueue(store)
    stub_model(monkeypatch, response)
    assert flush_service.process_jobs(store, job_id=job_id) == 1
    job = store.jobs()[0]
    assert job["status"] == "failed"
    assert job["context"] == "Conversation context"
    assert job["last_error"]
    assert store.list_sources() == []


def test_explicit_flush_ok_completes_without_source(store, monkeypatch) -> None:
    job_id = enqueue(store)
    calls = stub_model(monkeypatch, "FLUSH_OK")
    assert flush_service.process_jobs(store, job_id=job_id) == 0
    assert store.jobs()[0]["status"] == "done"
    assert store.list_sources() == []
    assert len(calls) == 1


def test_export_failure_never_reexecutes_completed_model(store, monkeypatch) -> None:
    job_id = enqueue(store)
    calls = stub_model(monkeypatch)
    original_export = flush_service.export_memory
    def failed_export(*args, **kwargs):
        raise ExportConflict("Externally modified exports")
    monkeypatch.setattr(flush_service, "export_memory", failed_export)
    assert flush_service.process_jobs(store, job_id=job_id) == 1
    assert store.jobs()[0]["status"] == "done"
    monkeypatch.setattr(flush_service, "export_memory", original_export)
    assert flush_service.process_jobs(store, job_id=job_id) == 0
    assert len(calls) == 1
    assert (store.root / "daily/2026-08-30.md").exists()


def test_provider_failure_stops_batch_and_cools_down_other_workers(store, monkeypatch) -> None:
    first = enqueue(store)
    second = enqueue(store, "s2")
    calls = []
    async def failed(*args, **kwargs):
        calls.append(1)
        raise RuntimeError("Provider unavailable")
    monkeypatch.setattr(flush_service, "call_readonly_model", failed)
    assert flush_service.process_jobs(store, job_id=first, limit=20) == 1
    assert flush_service.process_jobs(store, job_id=second, limit=20) == 1
    jobs = {job["id"]: job for job in store.jobs()}
    assert jobs[first]["attempts"] == 1
    assert jobs[second]["attempts"] == 0
    assert calls == [1]
    stub_model(monkeypatch)
    assert flush_service.process_jobs(store, job_id=second, force=True) == 0


def test_provider_outage_does_not_permanently_quarantine_valid_context(store, monkeypatch):
    job_id = enqueue(store)
    async def failed(*args, **kwargs):
        raise RuntimeError("Provider unavailable")
    monkeypatch.setattr(flush_service, "call_readonly_model", failed)
    for _ in range(4):
        assert flush_service.process_jobs(store, job_id=job_id, force=True) == 1
    assert store.jobs()[0]["status"] == "failed"


def test_quarantine_counts_only_malformed_responses_not_provider_failures(store, monkeypatch):
    job_id = enqueue(store, malformed_attempts=999)
    async def failed(*args, **kwargs):
        raise RuntimeError("Provider unavailable")
    monkeypatch.setattr(flush_service, "call_readonly_model", failed)
    for _ in range(2):
        assert flush_service.process_jobs(store, job_id=job_id, force=True) == 1
    stub_model(monkeypatch, "malformed")
    assert flush_service.process_jobs(store, job_id=job_id, force=True) == 1
    assert store.jobs()[0]["status"] == "failed"
    assert flush_service.process_jobs(store, job_id=job_id, force=True) == 1
    assert store.jobs()[0]["status"] == "failed"
    assert flush_service.process_jobs(store, job_id=job_id, force=True) == 1
    assert store.jobs()[0]["status"] == "quarantined"
    assert store.jobs()[0]["attempts"] == 5


def test_rejected_lease_cannot_increment_the_malformed_counter(store):
    job_id = enqueue(store)
    for expected_status in ("failed", "failed", "quarantined"):
        lease = store.claim_job(job_id, force=True)
        with pytest.raises(StoreConflict):
            store.fail_job(job_id, "not-this-lease", "Malformed response", malformed_limit=3)
        store.fail_job(job_id, lease["lease_token"], "Malformed response", malformed_limit=3)
        assert store.jobs()[0]["status"] == expected_status


def test_positional_bridge_reuses_migrated_recovery_range(store, monkeypatch):
    from memory_migrate import import_recovery_contexts
    path = store.root / "scripts/import-flush-019fd1a2-ea5e-7911-893c-a952b63155fa-3-7-20260905-120000.md"
    path.write_text("Context to preserve\r\n")
    import_recovery_contexts(store, store.root)
    calls = stub_model(monkeypatch)
    assert flush_service.main([str(path), "019fd1a2-ea5e-7911-893c-a952b63155fa", "--root", str(store.root)]) == 0
    assert len(store.jobs()) == 1
    assert len(calls) == 1
    assert path.exists()


def test_expired_job_lease_is_recovered(store, monkeypatch) -> None:
    job_id = enqueue(store)
    store.claim_job(job_id)
    with store.transaction() as connection:
        connection.execute("UPDATE jobs SET lease_until=0 WHERE id=?", (job_id,))
    stub_model(monkeypatch)
    assert flush_service.process_jobs(store, job_id=job_id) == 0
    assert store.jobs()[0]["attempts"] == 2
    assert store.jobs()[0]["status"] == "done"


def test_two_workers_do_not_duplicate_model_call_or_source(store, monkeypatch) -> None:
    job_id = enqueue(store)
    calls = stub_model(monkeypatch)
    with ThreadPoolExecutor(max_workers=2) as pool:
        statuses = list(pool.map(lambda _: flush_service.process_jobs(store, job_id=job_id), range(2)))
    assert statuses == [0, 0]
    assert len(calls) == 1
    assert store.read_source("daily/2026-08-30.md").count(SAVED) == 1


def test_cli_preserves_and_deduplicates_positional_context_file(store, monkeypatch) -> None:
    path = store.root / "context.md"
    path.write_text("Conversation context")
    calls = stub_model(monkeypatch)
    arguments = [str(path), "s1", "--root", str(store.root), "--captured-at", "2026-08-29T18:00:00+05:00"]
    assert flush_service.main(arguments) == 0
    assert flush_service.main(arguments) == 0
    assert path.read_text() == "Conversation context"
    assert len(store.jobs()) == 1
    assert len(calls) == 1
    assert store.read_source("daily/2026-08-29.md") is not None


def test_drain_honors_limit_and_retry_failed_excludes_pending(store, monkeypatch) -> None:
    for index in range(3):
        enqueue(store, f"s{index}")
    calls = stub_model(monkeypatch)
    assert flush_service.main(["--root", str(store.root), "--drain", "--limit", "2"]) == 0
    assert len(calls) == 2
    assert flush_service.main(["--root", str(store.root), "--retry-failed"]) == 0
    assert len(calls) == 2
    assert sum(job["status"] == "pending" for job in store.jobs()) == 1


def test_repeated_malformed_job_is_quarantined(store, monkeypatch) -> None:
    job_id = enqueue(store)
    stub_model(monkeypatch, "malformed")
    for _ in range(3):
        assert flush_service.process_jobs(store, job_id=job_id, force=True) == 1
    assert store.jobs()[0]["status"] == "quarantined"
    assert flush_service.process_jobs(store, job_id=job_id, force=True) == 0
    assert store.jobs()[0]["attempts"] == 3


def test_compilation_args_use_canonical_backlog_and_evening_cutoff(store) -> None:
    store.import_source("daily/2026-09-05.md", "Today's source")
    before = datetime.fromisoformat("2026-09-05T21:00:00+05:00")
    after = datetime.fromisoformat("2026-09-05T22:00:00+05:00")
    assert flush_service.compilation_args(store, before) is None
    assert flush_service.compilation_args(store, after) == []
    store.import_source("daily/2026-09-04.md", "Yesterday's source")
    assert flush_service.compilation_args(store, before) == ["--skip-today"]


def test_compile_trigger_is_detached_and_debounced_across_invocations(store, monkeypatch) -> None:
    script = store.root / "scripts/compile.py"
    script.write_text("# Compiler stub")
    store.import_source("daily/2026-01-01.md", "Past-day backlog")
    calls = []
    def spawn(command, **kwargs):
        calls.append((command, kwargs))
        assert not kwargs["stdout"].closed
        return SimpleNamespace(pid=1234)
    monkeypatch.setattr(flush_service.subprocess, "Popen", spawn)
    assert flush_service.maybe_trigger_compilation(store) == 0
    assert flush_service.maybe_trigger_compilation(store) == 0
    assert len(calls) == 1
    assert calls[0][0][:2] == [flush_service.sys.executable, str(script)]
    assert calls[0][1]["start_new_session"] is True
    assert calls[0][1]["stdin"] == flush_service.subprocess.DEVNULL
    assert calls[0][1]["env"]["MEMORY_COMPILER_INTERNAL"] == "1"
    assert calls[0][1]["stdout"].closed
    assert store.get_state("compile_trigger")["pid"] == 1234


def test_main_triggers_compile_after_success_and_never_after_failure(store, monkeypatch) -> None:
    job_id = enqueue(store)
    stub_model(monkeypatch)
    triggered = []
    monkeypatch.setattr(flush_service, "maybe_trigger_compilation", lambda current: triggered.append(current.root) or 0)
    assert flush_service.main(["--root", str(store.root), "--job-id", job_id]) == 0
    assert triggered == [store.root]
    second = enqueue(store, "second")
    stub_model(monkeypatch, "invalid")
    assert flush_service.main(["--root", str(store.root), "--job-id", second]) == 1
    assert triggered == [store.root]


def test_compiler_spawn_failure_keeps_backlog_and_does_not_record_successful_trigger(store, monkeypatch) -> None:
    (store.root / "scripts/compile.py").write_text("# Compiler stub")
    store.import_source("daily/2026-01-01.md", "Past-day backlog")
    def failed(*args, **kwargs):
        raise OSError("Cannot spawn")
    monkeypatch.setattr(flush_service.subprocess, "Popen", failed)
    assert flush_service.maybe_trigger_compilation(store) == 1
    assert store.get_state("compile_trigger", {}) == {}
    assert store.get_state("pipeline")["last_compile"]["status"] == "failed"
    assert store.read_source("daily/2026-01-01.md") == "Past-day backlog"


def test_compile_trigger_debounce_does_not_hide_work_forever(store, monkeypatch) -> None:
    (store.root / "scripts/compile.py").write_text("# Compiler stub")
    store.import_source("daily/2026-01-01.md", "Past-day backlog")
    store.set_state("compile_trigger", {"requested_at": flush_service.time.time() - 31 * 60})
    calls = []
    monkeypatch.setattr(flush_service.subprocess, "Popen", lambda *args, **kwargs: calls.append(args) or SimpleNamespace(pid=1234))
    assert flush_service.maybe_trigger_compilation(store) == 0
    assert len(calls) == 1
