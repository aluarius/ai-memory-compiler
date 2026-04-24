from __future__ import annotations

import asyncio

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
