from __future__ import annotations

from pathlib import Path

from session_utils import detect_transcript_format, parse_transcript


FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"


def test_detect_transcript_format_for_claude_fixture() -> None:
    transcript = FIXTURES_DIR / "claude-session.jsonl"

    assert detect_transcript_format(transcript) == "claude_jsonl"


def test_parse_claude_transcript_extracts_recent_turns() -> None:
    transcript = FIXTURES_DIR / "claude-session.jsonl"

    parsed = parse_transcript(transcript, max_turns=4, max_chars=2_000)

    assert parsed.format == "claude_jsonl"
    assert parsed.turn_count == 4
    assert "**User:** Add Codex import support too." in parsed.context
    assert "**Assistant:** Support manual transcript import first" in parsed.context


def test_detect_transcript_format_for_codex_fixture() -> None:
    transcript = FIXTURES_DIR / "codex-session.jsonl"

    assert detect_transcript_format(transcript) == "codex_jsonl"


def test_parse_codex_transcript_extracts_metadata_and_messages() -> None:
    transcript = FIXTURES_DIR / "codex-session.jsonl"

    parsed = parse_transcript(transcript, max_turns=6, max_chars=2_000)

    assert parsed.format == "codex_jsonl"
    assert parsed.turn_count == 2
    assert parsed.session_id == "rollout-abc123"
    assert parsed.provider == "openai"
    assert parsed.model == "gpt-5.4"
    assert parsed.cwd == "/Users/exmac/Desktop/ideas/claude-memory-compiler"
    assert parsed.source == "cli=codex,mode=interactive"
    assert "System guidance should not be imported." not in parsed.context
    assert "**User:** Can we reuse the same memory pipeline for Codex?" in parsed.context
    assert "**Assistant:** Yes. Import the transcript into the shared flush and compile flow." in parsed.context
