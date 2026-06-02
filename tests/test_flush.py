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
