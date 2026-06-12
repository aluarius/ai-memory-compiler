from __future__ import annotations

import argparse
import asyncio
from pathlib import Path

import flush


def test_run_flush_returns_flush_error_when_codex_runtime_fails(monkeypatch) -> None:
    monkeypatch.setattr(flush, "get_task_runtime", lambda _: "codex")
    monkeypatch.setattr(flush, "get_codex_model", lambda: "gpt-5.4")

    def boom(*args, **kwargs):
        raise RuntimeError("codex unavailable")

    monkeypatch.setattr(flush, "run_codex_prompt", boom)

    result = asyncio.run(flush.run_flush("context"))

    assert result == "FLUSH_ERROR: RuntimeError: codex unavailable"


def test_clean_flush_response_strips_transcript_scaffolding() -> None:
    content = """Attempting to read the plan for the full review context:

**Context:** Project work

**Key Exchanges:**
- Found a bug
"""

    assert flush.clean_flush_response(content) == """**Context:** Project work

**Key Exchanges:**
- Found a bug"""


def test_append_to_daily_log_creates_sessions_only_skeleton(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(flush, "DAILY_DIR", tmp_path)
    monkeypatch.setattr(flush, "DAILY_LOG_LOCK_FILE", tmp_path / ".daily.lock")

    metadata = flush.SessionMetadata(
        session_id="session-1",
        agent="codex",
        provider="openai",
    )

    flush.append_to_daily_log("**Context:** Saved item", metadata)

    daily_logs = list(tmp_path.glob("*.md"))
    assert len(daily_logs) == 1
    content = daily_logs[0].read_text(encoding="utf-8")
    assert content.startswith("# Daily Log:")
    assert "\n## Sessions\n\n### Session" in content
    assert "## Memory Maintenance" not in content


def test_preserve_failed_context_moves_file_to_failed_flush_dir(
    tmp_path: Path, monkeypatch
) -> None:
    failed_dir = tmp_path / "failed"
    context_file = tmp_path / "session-flush-session-1.md"
    context_file.write_text("context", encoding="utf-8")
    monkeypatch.setattr(flush, "FAILED_FLUSH_DIR", failed_dir)

    preserved = flush.preserve_failed_context(context_file)

    assert preserved is not None
    assert preserved == failed_dir / "session-flush-session-1.md"
    assert preserved.read_text(encoding="utf-8") == "context"
    assert not context_file.exists()


def test_main_preserves_failed_context_without_marking_flushed_or_compiling(
    tmp_path: Path, monkeypatch
) -> None:
    context_file = tmp_path / "session-flush-session-1.md"
    context_file.write_text("context", encoding="utf-8")
    failed_dir = tmp_path / "failed"
    calls: list[str] = []

    monkeypatch.setattr(
        flush,
        "parse_args",
        lambda: argparse.Namespace(
            context_file=context_file,
            session_id="session-1",
            agent="codex",
            provider="openai",
            model="gpt-5",
            cwd="/repo",
            source="test",
            retry_failed=False,
        ),
    )
    monkeypatch.setattr(flush, "cleanup_old_temp_files", lambda: calls.append("cleanup"))
    monkeypatch.setattr(flush, "was_recently_flushed", lambda *_: False)
    monkeypatch.setattr(flush, "append_runtime_event", lambda *_: calls.append("event"))
    monkeypatch.setattr(flush, "remember_flush", lambda *_: calls.append("remember"))
    monkeypatch.setattr(flush, "maybe_trigger_compilation", lambda: calls.append("compile"))
    monkeypatch.setattr(flush, "FAILED_FLUSH_DIR", failed_dir)

    async def fail_flush(context: str) -> str:
        assert context == "context"
        return "FLUSH_ERROR: RuntimeError: boom"

    monkeypatch.setattr(flush, "run_flush", fail_flush)

    flush.main()

    assert (failed_dir / context_file.name).read_text(encoding="utf-8") == "context"
    assert not context_file.exists()
    assert "event" in calls
    assert "remember" not in calls
    assert "compile" not in calls


# ---------------------------------------------------------------------------
# --retry-failed lifecycle
# ---------------------------------------------------------------------------

UUID_A = "019e2b61-ebf1-73b3-a5cd-92b50c8921d8"
UUID_B = "40b4f505-6d7f-472a-a40d-adbbb9c820e2"


def test_extract_session_id_handles_all_prefixes() -> None:
    assert flush.extract_session_id(f"session-flush-{UUID_B}-20260603-1200.md") == UUID_B
    assert flush.extract_session_id(f"flush-context-{UUID_B}.md") == UUID_B
    assert flush.extract_session_id(f"import-flush-{UUID_A}-20260606-004107.md") == UUID_A
    assert flush.extract_session_id("garbage-name.md") is None


def _setup_failed_dir(tmp_path, monkeypatch):
    failed_dir = tmp_path / "failed-flushes"
    failed_dir.mkdir()
    monkeypatch.setattr(flush, "FAILED_FLUSH_DIR", failed_dir)
    monkeypatch.setattr(flush, "PERMANENT_FAILED_DIR", failed_dir / "permanent")
    monkeypatch.setattr(flush, "RETRY_STATE_FILE", failed_dir / "retry-state.json")
    monkeypatch.setattr(flush, "DAILY_DIR", tmp_path / "daily")
    monkeypatch.setattr(flush, "DAILY_LOG_LOCK_FILE", tmp_path / ".daily.lock")
    return failed_dir


def test_retry_failed_dedups_per_session_and_deletes_all_copies_on_success(
    tmp_path, monkeypatch
) -> None:
    failed_dir = _setup_failed_dir(tmp_path, monkeypatch)
    import os
    import time as time_mod

    old = failed_dir / f"import-flush-{UUID_A}-20260606-004107.md"
    new = failed_dir / f"import-flush-{UUID_A}-20260606-021244.md"
    old.write_text("old copy", encoding="utf-8")
    new.write_text("new copy", encoding="utf-8")
    now = time_mod.time()
    os.utime(old, (now - 100, now - 100))
    os.utime(new, (now, now))

    seen_contexts = []

    async def fake_flush(context: str) -> str:
        seen_contexts.append(context)
        return "**Context:** recovered content"

    monkeypatch.setattr(flush, "run_flush", fake_flush)

    recovered = flush.retry_failed_flushes()

    assert recovered == 1
    assert seen_contexts == ["new copy"]  # newest copy only
    assert not old.exists() and not new.exists()
    daily = list((tmp_path / "daily").glob("*.md"))
    assert len(daily) == 1
    assert "recovered content" in daily[0].read_text(encoding="utf-8")


def test_retry_failed_flush_ok_deletes_without_logging(tmp_path, monkeypatch) -> None:
    failed_dir = _setup_failed_dir(tmp_path, monkeypatch)
    f = failed_dir / f"session-flush-{UUID_B}.md"
    f.write_text("noise", encoding="utf-8")

    async def fake_flush(context: str) -> str:
        return "FLUSH_OK"

    monkeypatch.setattr(flush, "run_flush", fake_flush)

    recovered = flush.retry_failed_flushes()

    assert recovered == 1
    assert not f.exists()
    assert not (tmp_path / "daily").exists()


def test_retry_failed_increments_attempts_then_moves_to_permanent(
    tmp_path, monkeypatch
) -> None:
    failed_dir = _setup_failed_dir(tmp_path, monkeypatch)
    f = failed_dir / f"flush-context-{UUID_B}.md"
    f.write_text("never works", encoding="utf-8")

    async def fail_flush(context: str) -> str:
        return "FLUSH_ERROR: RuntimeError: nope"

    monkeypatch.setattr(flush, "run_flush", fail_flush)

    # force=True models the nightly maintenance cadence (24h apart, no cooldown)
    for expected_attempts in (1, 2, 3):
        recovered = flush.retry_failed_flushes(force=True)
        assert recovered == 0
        state = flush.load_retry_state()
        assert state[UUID_B]["attempts"] == expected_attempts
        assert f.exists()

    # Fourth run: over the limit — moved to permanent, state cleared.
    recovered = flush.retry_failed_flushes(force=True)
    assert recovered == 0
    assert not f.exists()
    assert (failed_dir / "permanent" / f.name).exists()
    assert UUID_B not in flush.load_retry_state()


def test_retry_failed_skips_unparseable_and_empty_files(tmp_path, monkeypatch) -> None:
    failed_dir = _setup_failed_dir(tmp_path, monkeypatch)
    unparseable = failed_dir / "garbage-name.md"
    unparseable.write_text("content", encoding="utf-8")
    empty = failed_dir / f"session-flush-{UUID_A}.md"
    empty.write_text("   \n", encoding="utf-8")

    async def fake_flush(context: str) -> str:
        raise AssertionError("should not be called")

    monkeypatch.setattr(flush, "run_flush", fake_flush)

    recovered = flush.retry_failed_flushes()

    assert recovered == 0
    assert unparseable.exists()  # left for manual inspection
    assert not empty.exists()  # dropped


def test_preserve_failed_context_replaces_older_copies_of_same_session(
    tmp_path, monkeypatch
) -> None:
    failed_dir = tmp_path / "failed"
    failed_dir.mkdir()
    monkeypatch.setattr(flush, "FAILED_FLUSH_DIR", failed_dir)

    old_copy = failed_dir / f"import-flush-{UUID_A}-20260606-004107.md"
    old_copy.write_text("old snapshot", encoding="utf-8")

    fresh = tmp_path / f"import-flush-{UUID_A}-20260612-120000.md"
    fresh.write_text("newest snapshot", encoding="utf-8")

    preserved = flush.preserve_failed_context(fresh)

    assert preserved is not None
    assert not old_copy.exists()  # superseded copy removed
    remaining = list(failed_dir.glob("*.md"))
    assert len(remaining) == 1
    assert remaining[0].read_text(encoding="utf-8") == "newest snapshot"


def test_retry_failed_respects_limit(tmp_path, monkeypatch) -> None:
    failed_dir = _setup_failed_dir(tmp_path, monkeypatch)
    a = failed_dir / f"session-flush-{UUID_A}.md"
    b = failed_dir / f"session-flush-{UUID_B}.md"
    a.write_text("content a", encoding="utf-8")
    b.write_text("content b", encoding="utf-8")

    calls = []

    async def fake_flush(context: str) -> str:
        calls.append(context)
        return "FLUSH_OK"

    monkeypatch.setattr(flush, "run_flush", fake_flush)

    recovered = flush.retry_failed_flushes(limit=1)

    assert recovered == 1
    assert len(calls) == 1
    # Exactly one of the two contexts remains for the next pass
    assert len(list(failed_dir.glob("session-flush-*.md"))) == 1


def test_has_stale_past_logs_ignores_today(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(flush, "DAILY_DIR", tmp_path)
    (tmp_path / "2026-06-10.md").write_text("old", encoding="utf-8")
    (tmp_path / "2026-06-12.md").write_text("today", encoding="utf-8")

    ingested = {}  # nothing compiled
    assert flush._has_stale_past_logs(ingested, "2026-06-12.md") is True

    # Past log compiled with matching hash, today still uncompiled -> no backlog
    from utils import file_hash as fh
    ingested = {"2026-06-10.md": {"hash": fh(tmp_path / "2026-06-10.md")}}
    assert flush._has_stale_past_logs(ingested, "2026-06-12.md") is False


def test_retry_failed_skips_sessions_in_cooldown_unless_forced(
    tmp_path, monkeypatch
) -> None:
    from datetime import datetime, timezone

    failed_dir = _setup_failed_dir(tmp_path, monkeypatch)
    f = failed_dir / f"session-flush-{UUID_A}.md"
    f.write_text("content", encoding="utf-8")

    # Mark a recent attempt (now) in retry state
    flush.save_retry_state(
        {
            UUID_A: {
                "attempts": 1,
                "last_attempt": datetime.now(timezone.utc).astimezone().isoformat(
                    timespec="seconds"
                ),
            }
        }
    )

    calls = []

    async def fake_flush(context: str) -> str:
        calls.append(context)
        return "FLUSH_OK"

    monkeypatch.setattr(flush, "run_flush", fake_flush)

    # Cooldown active: skipped
    assert flush.retry_failed_flushes() == 0
    assert calls == []
    assert f.exists()

    # Forced: retried
    assert flush.retry_failed_flushes(force=True) == 1
    assert len(calls) == 1
    assert not f.exists()
