from __future__ import annotations

import json
from pathlib import Path

from session_utils import codex_message_ranges, detect_transcript_format, parse_transcript


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


def test_detect_transcript_format_skips_claude_cli_metadata_preamble() -> None:
    transcript = FIXTURES_DIR / "claude-session-with-meta.jsonl"

    assert detect_transcript_format(transcript) == "claude_jsonl"


def test_parse_claude_transcript_ignores_metadata_preamble() -> None:
    transcript = FIXTURES_DIR / "claude-session-with-meta.jsonl"

    parsed = parse_transcript(transcript, max_turns=10, max_chars=2_000)

    assert parsed.format == "claude_jsonl"
    assert parsed.turn_count == 2
    assert "**User:** How should we structure the compiler?" in parsed.context
    assert "**Assistant:** Keep the scripts flat." in parsed.context


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
    assert parsed.cwd == "/Users/exmac/Desktop/ideas/ai-memory-compiler"
    assert parsed.source == "cli=codex,mode=interactive"
    assert "System guidance should not be imported." not in parsed.context
    assert "**User:** Can we reuse the same memory pipeline for Codex?" in parsed.context
    assert "**Assistant:** Yes. Import the transcript into the shared flush and compile flow." in parsed.context


def test_parse_codex_transcript_extracts_only_the_checkpoint_delta() -> None:
    transcript = FIXTURES_DIR / "codex-session.jsonl"

    parsed = parse_transcript(
        transcript,
        max_turns=6,
        max_chars=2_000,
        after_message_count=1,
        until_message_count=2,
    )

    assert parsed.turn_count == 1
    assert parsed.message_count == 2
    assert "**User:** Can we reuse the same memory pipeline for Codex?" not in parsed.context
    assert "**Assistant:** Yes. Import the transcript into the shared flush and compile flow." in parsed.context


def test_codex_message_ranges_split_the_unseen_prefix_without_gaps(tmp_path: Path) -> None:
    transcript = tmp_path / "rollout-test.jsonl"
    entries = [
        {
            "type": "session_meta",
            "payload": {"id": "session-1", "model_provider": "openai"},
        },
    ]
    entries.extend(
        {
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "user" if number % 2 else "assistant",
                "content": [{"type": "input_text", "text": f"message {number}"}],
            },
        }
        for number in range(1, 8)
    )
    transcript.write_text(
        "\n".join(json.dumps(entry) for entry in entries) + "\n", encoding="utf-8"
    )

    message_count, ranges = codex_message_ranges(
        transcript,
        after_message_count=1,
        max_turns=2,
        max_chars=2_000,
    )

    assert message_count == 7
    assert ranges == [(1, 3), (3, 5), (5, 7)]
