from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import import_session
import pytest

from capture_service import capture_transcript
from memory_store import MemoryStore
from model_runtime import ModelResult


FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"


@pytest.fixture(autouse=True)
def isolated_root(tmp_path, monkeypatch):
    monkeypatch.setattr(import_session, "ROOT_DIR", tmp_path)


def test_import_session_writes_only_the_reserved_codex_message_delta(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(
        import_session,
        "parse_args",
        lambda: argparse.Namespace(
            transcript=FIXTURES_DIR / "codex-session.jsonl",
            session_id="rollout-abc123",
            agent="codex",
            provider="openai",
            model=None,
            cwd=None,
            source="hook:stop",
            after_message_count=1,
            until_message_count=2,
        ),
    )
    monkeypatch.setattr(import_session, "SCRIPTS_DIR", tmp_path)
    captured: list[list[str]] = []

    def fake_run(cmd, *args, **kwargs):
        captured.append(cmd)
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(import_session.subprocess, "run", fake_run)

    assert import_session.main() == 0

    contexts = list(tmp_path.glob("import-flush-*.md"))
    assert len(contexts) == 1
    context = contexts[0].read_text(encoding="utf-8")
    assert "Can we reuse the same memory pipeline for Codex?" not in context
    assert "Import the transcript into the shared flush and compile flow." in context
    assert captured[0][0] == sys.executable


def test_import_session_preserves_context_when_the_flush_process_cannot_start(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(
        import_session,
        "parse_args",
        lambda: argparse.Namespace(
            transcript=FIXTURES_DIR / "codex-session.jsonl",
            session_id="019e2b61-ebf1-73b3-a5cd-92b50c8921d8",
            agent="codex",
            provider="openai",
            model=None,
            cwd=None,
            source="hook:stop",
            after_message_count=0,
            until_message_count=2,
        ),
    )
    monkeypatch.setattr(import_session, "SCRIPTS_DIR", tmp_path)
    failed_dir = tmp_path / "failed-flushes"
    monkeypatch.setattr(import_session, "FAILED_FLUSH_DIR", failed_dir, raising=False)
    monkeypatch.setattr(
        import_session.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(returncode=1),
    )

    assert import_session.main() == 1

    preserved = list(failed_dir.glob("import-flush-*.md"))
    assert len(preserved) == 1
    assert "Can we reuse the same memory pipeline for Codex?" in preserved[0].read_text(
        encoding="utf-8"
    )


@pytest.fixture
def canonical_import(tmp_path, monkeypatch):
    store = MemoryStore(tmp_path)
    store.initialize()
    transcript = tmp_path / "session.jsonl"
    transcript.write_text(json.dumps({
        "type": "response_item", "payload": {
            "type": "message", "role": "user", "content": "Preserve the SQLite decision",
        },
    }) + "\n")
    args = argparse.Namespace(
        transcript=transcript, session_id="s1", agent="codex", provider="openai",
        model=None, cwd="/project", source="import", after_message_count=0,
        until_message_count=None,
    )
    monkeypatch.setattr(import_session, "parse_args", lambda: args)
    async def forbidden(*args, **kwargs):
        raise AssertionError("Tests must stub every model response")
    monkeypatch.setattr("flush_service.call_readonly_model", forbidden)
    def forbidden_spawn(*args, **kwargs):
        raise AssertionError("Tests must stub every process launch")
    monkeypatch.setattr("flush_service.subprocess.Popen", forbidden_spawn)
    return store, args


def test_canonical_import_retries_its_failed_capture_without_recapturing(canonical_import, monkeypatch):
    store, args = canonical_import
    async def malformed(*args, **kwargs):
        return ModelResult(text="Unstructured response")
    monkeypatch.setattr("flush_service.call_readonly_model", malformed)
    assert import_session.main() == 1
    job_id = store.jobs()[0]["id"]
    with store.transaction() as conn:
        conn.execute("UPDATE jobs SET next_attempt=0 WHERE id=?", (job_id,))
    async def success(*args, **kwargs):
        return ModelResult(text="**Context:** Preserved the SQLite decision")
    monkeypatch.setattr("flush_service.call_readonly_model", success)
    assert import_session.main() == 0
    assert [(job["id"], job["status"], job["attempts"]) for job in store.jobs()] == [(job_id, "done", 2)]


@pytest.mark.parametrize("status", ["cooldown", "quarantined", "running"])
def test_canonical_import_reports_unfinished_capture_instead_of_false_success(canonical_import, status):
    store, args = canonical_import
    job_id = capture_transcript(store, args.transcript, {"agent": "codex", "session_id": "s1"})[0]
    lease = store.claim_job(job_id)
    if status != "running":
        store.fail_job(job_id, lease["lease_token"], "Synthetic failure", quarantine=status == "quarantined")
    assert import_session.main() == 1
    assert store.jobs()[0]["attempts"] == 1


def test_canonical_import_retry_leaves_other_session_transcript_and_range_untouched(canonical_import, monkeypatch):
    store, args = canonical_import
    metadata = {"agent": "codex", "session_id": "s1", "transcript_path": str(args.transcript.resolve())}
    wanted = capture_transcript(store, args.transcript, metadata)[0]
    unrelated = [store.enqueue("Unrelated captured context", {**metadata, **override}, identity=str(index)) for index, override in enumerate([
        {"session_id": "another-session", "after_message_count": 0, "until_message_count": 1},
        {"transcript_path": str(store.root / "other.jsonl"), "after_message_count": 0, "until_message_count": 1},
        {"agent": "claude_code", "after_message_count": 0, "until_message_count": 1},
        {"after_message_count": 1, "until_message_count": 2},
    ])]
    args.until_message_count = 1
    async def success(*args, **kwargs):
        return ModelResult(text="FLUSH_OK")
    monkeypatch.setattr("flush_service.call_readonly_model", success)
    assert import_session.main() == 0
    jobs = {job["id"]: job for job in store.jobs()}
    assert jobs[wanted]["status"] == "done"
    assert all(jobs[job_id]["status"] == "pending" for job_id in unrelated)
    assert all(jobs[job_id]["attempts"] == 0 for job_id in unrelated)


def test_canonical_import_does_not_merge_scopes_with_identical_redacted_metadata(canonical_import, monkeypatch):
    store, args = canonical_import
    args.session_id = "password=first-synthetic-secret"
    wanted = capture_transcript(store, args.transcript, {"agent": "codex", "session_id": args.session_id})[0]
    other = capture_transcript(store, args.transcript, {"agent": "codex", "session_id": "password=second-synthetic-secret"})[0]
    async def success(*args, **kwargs):
        return ModelResult(text="FLUSH_OK")
    monkeypatch.setattr("flush_service.call_readonly_model", success)
    assert import_session.main() == 0
    jobs = {job["id"]: job for job in store.jobs()}
    assert jobs[wanted]["metadata"]["session_id"] == jobs[other]["metadata"]["session_id"]
    assert jobs[wanted]["status"] == "done"
    assert jobs[other]["status"] == "pending"
    assert jobs[other]["attempts"] == 0


def test_canonical_import_success_triggers_eligible_compilation(canonical_import, monkeypatch):
    store, args = canonical_import
    (store.root / "scripts/compile.py").write_text("# Detached compiler test stub\n")
    monkeypatch.setattr("capture_service.timestamp", lambda: "2026-01-01T23:00:00+05:00")
    async def success(*args, **kwargs):
        return ModelResult(text="**Context:** Preserved the SQLite decision")
    monkeypatch.setattr("flush_service.call_readonly_model", success)
    monkeypatch.setattr("flush_service.subprocess.Popen", lambda *a, **kw: SimpleNamespace(pid=1234))
    assert import_session.main() == 0
    assert store.jobs()[0]["status"] == "done"
    assert store.get_state("compile_trigger", {}).get("pid") == 1234


def test_reimporting_completed_explicit_range_ignores_old_retry_deadline(canonical_import, monkeypatch):
    from flush_service import process_jobs
    store, args = canonical_import
    args.until_message_count = 1
    job_id = capture_transcript(store, args.transcript, {"agent": "codex", "session_id": "s1"},
                                until_message_count=1)[0]
    lease = store.claim_job(job_id)
    store.fail_job(job_id, lease["lease_token"], "Old failure", delay_seconds=3600)
    async def success(*args, **kwargs):
        return ModelResult(text="FLUSH_OK")
    monkeypatch.setattr("flush_service.call_readonly_model", success)
    assert process_jobs(store, job_id=job_id, force=True) == 0
    assert import_session.main() == 0
    assert store.jobs()[0]["attempts"] == 2


def test_completed_explicit_import_is_not_blocked_by_unrelated_worker_cooldown(canonical_import):
    store, args = canonical_import
    args.until_message_count = 1
    wanted = capture_transcript(store, args.transcript, {"agent": "codex", "session_id": "s1"},
                                until_message_count=1)[0]
    lease = store.claim_job(wanted)
    store.complete_job(wanted, lease["lease_token"], "", "daily/2026-01-01.md")
    unrelated = store.enqueue("Unrelated context", {"session_id": "other"}, identity="other")
    lease = store.claim_job(unrelated)
    store.fail_job(unrelated, lease["lease_token"], "Provider unavailable", delay_seconds=3600)
    cooldown = import_session.time.time() + 3600
    store.set_state("flush_worker", {"cooldown_until": cooldown})

    assert import_session.main() == 0
    jobs = {job["id"]: job for job in store.jobs()}
    assert (jobs[wanted]["status"], jobs[wanted]["attempts"]) == ("done", 1)
    assert (jobs[unrelated]["status"], jobs[unrelated]["attempts"]) == ("failed", 1)
    assert store.get_state("flush_worker")["cooldown_until"] == cooldown
