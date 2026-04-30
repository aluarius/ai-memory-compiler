from __future__ import annotations

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
