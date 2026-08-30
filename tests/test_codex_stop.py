from __future__ import annotations

import importlib.util
import json
import os
import sys
from pathlib import Path


def load_codex_stop_module():
    root = Path(__file__).resolve().parent.parent
    module_path = root / "hooks" / "codex-stop.py"
    spec = importlib.util.spec_from_file_location("codex_stop_hook", module_path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    previous = os.environ.pop("CLAUDE_INVOKED_BY", None)
    try:
        spec.loader.exec_module(module)
    finally:
        if previous is not None:
            os.environ["CLAUDE_INVOKED_BY"] = previous
    return module


def test_parse_hook_input_rejects_invalid_json() -> None:
    codex_stop = load_codex_stop_module()

    assert codex_stop.parse_hook_input("not json") == {}
    assert codex_stop.parse_hook_input("") == {}


def test_resolve_transcript_from_hook_prefers_hook_payload(tmp_path: Path) -> None:
    codex_stop = load_codex_stop_module()
    transcript = tmp_path / "rollout-test.jsonl"
    transcript.write_text(
        json.dumps(
            {
                "type": "session_meta",
                "payload": {
                    "id": "rollout-123",
                    "cwd": "/fallback/cwd",
                    "model_provider": "openai",
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )

    resolved_transcript, meta = codex_stop.resolve_transcript_from_hook(
        {
            "transcript_path": str(transcript),
            "session_id": "session-from-hook",
            "cwd": "/repo",
            "model": "gpt-5.4",
            "turn_id": "turn-abc",
        }
    )

    assert resolved_transcript == transcript
    assert meta == {
        "session_id": "session-from-hook",
        "cwd": "/repo",
        "model": "gpt-5.4",
        "provider": "openai",
        "turn_id": "turn-abc",
        "source": "hook:stop",
    }


def test_build_import_key_uses_turn_id_when_available(tmp_path: Path) -> None:
    codex_stop = load_codex_stop_module()
    transcript = tmp_path / "rollout-test.jsonl"

    assert (
        codex_stop.build_import_key(
            session_id="session-1",
            turn_id="turn-1",
            transcript=transcript,
            transcript_mtime_ns=123,
        )
        == "session:session-1:turn:turn-1"
    )

    assert (
        codex_stop.build_import_key(
            session_id="session-1",
            turn_id="",
            transcript=transcript,
            transcript_mtime_ns=123,
        )
        == f"path:{transcript}:mtime:123"
    )


def test_should_skip_stop_event_on_continuation() -> None:
    codex_stop = load_codex_stop_module()

    assert codex_stop.should_skip_stop_event({"stop_hook_active": True}) is True
    assert codex_stop.should_skip_stop_event({"stop_hook_active": False}) is False


def test_claim_import_key_is_deduplicated(tmp_path: Path) -> None:
    codex_stop = load_codex_stop_module()
    codex_stop.DEDUP_FILE = tmp_path / ".last-codex-import.json"
    codex_stop.DEDUP_LOCK_FILE = tmp_path / ".locks" / "codex-stop.lock"

    assert codex_stop.claim_import_key("session:a:turn:b") is True
    assert codex_stop.claim_import_key("session:a:turn:b") is False


def test_claim_import_key_allows_distinct_turns_from_the_same_session(tmp_path: Path, monkeypatch) -> None:
    codex_stop = load_codex_stop_module()
    codex_stop.DEDUP_FILE = tmp_path / ".last-codex-import.json"
    codex_stop.DEDUP_LOCK_FILE = tmp_path / ".locks" / "codex-stop.lock"

    monkeypatch.setattr(codex_stop.time, "time", lambda: 1_000)

    assert codex_stop.claim_import_key("session:a:turn:1", session_id="a") is True
    assert codex_stop.claim_import_key("session:a:turn:2", session_id="a") is True
    assert codex_stop.claim_import_key("session:b:turn:1", session_id="b") is True


def test_reserve_import_uses_the_previous_message_checkpoint(tmp_path: Path, monkeypatch) -> None:
    codex_stop = load_codex_stop_module()
    codex_stop.DEDUP_FILE = tmp_path / ".last-codex-import.json"
    codex_stop.DEDUP_LOCK_FILE = tmp_path / ".locks" / "codex-stop.lock"
    monkeypatch.setattr(codex_stop.time, "time", lambda: 1_000)
    first = codex_stop.reserve_import("session:a:turn:1", session_id="a", message_count=4)

    assert first is not None
    assert first.after_message_count == 0
    assert first.until_message_count == 4

    second = codex_stop.reserve_import("session:a:turn:2", session_id="a", message_count=6)

    assert second is not None
    assert second.after_message_count == 4
    assert second.until_message_count == 6


def test_release_import_restores_the_checkpoint_after_spawn_failure(
    tmp_path: Path, monkeypatch
) -> None:
    codex_stop = load_codex_stop_module()
    codex_stop.DEDUP_FILE = tmp_path / ".last-codex-import.json"
    codex_stop.DEDUP_LOCK_FILE = tmp_path / ".locks" / "codex-stop.lock"
    monkeypatch.setattr(codex_stop.time, "time", lambda: 1_000)

    reservation = codex_stop.reserve_import(
        "session:a:turn:1", session_id="a", message_count=4
    )
    assert reservation is not None

    codex_stop.release_import("session:a:turn:1", session_id="a", reservation=reservation)
    retry = codex_stop.reserve_import("session:a:turn:2", session_id="a", message_count=4)

    assert retry is not None
    assert retry.after_message_count == 0
    assert retry.until_message_count == 4


def test_release_import_rewinds_a_later_reservation_to_avoid_losing_the_earlier_range(
    tmp_path: Path, monkeypatch
) -> None:
    codex_stop = load_codex_stop_module()
    codex_stop.DEDUP_FILE = tmp_path / ".last-codex-import.json"
    codex_stop.DEDUP_LOCK_FILE = tmp_path / ".locks" / "codex-stop.lock"
    monkeypatch.setattr(codex_stop.time, "time", lambda: 1_000)

    first = codex_stop.reserve_import("session:a:turn:1", session_id="a", message_count=4)
    second = codex_stop.reserve_import("session:a:turn:2", session_id="a", message_count=6)

    assert first is not None
    assert second is not None

    codex_stop.release_import("session:a:turn:1", session_id="a", reservation=first)
    retry = codex_stop.reserve_import("session:a:turn:3", session_id="a", message_count=6)

    assert retry is not None
    assert retry.after_message_count == 0
    assert retry.until_message_count == 6


def test_build_import_command_forwards_the_reserved_message_range(tmp_path: Path) -> None:
    codex_stop = load_codex_stop_module()
    transcript = tmp_path / "rollout-test.jsonl"

    cmd = codex_stop.build_import_command(
        transcript,
        {
            "session_id": "session-1",
            "provider": "openai",
            "source": "hook:stop",
            "after_message_count": 4,
            "until_message_count": 6,
        },
    )

    assert "--after-message-count" in cmd
    assert cmd[cmd.index("--after-message-count") + 1] == "4"
    assert "--until-message-count" in cmd
    assert cmd[cmd.index("--until-message-count") + 1] == "6"


def test_build_import_command_uses_the_hook_interpreter(tmp_path: Path) -> None:
    codex_stop = load_codex_stop_module()

    cmd = codex_stop.build_import_command(tmp_path / "rollout-test.jsonl", {})

    assert cmd[0] == sys.executable
