from __future__ import annotations

import importlib.util
import json
import os
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


def test_claim_import_key_rate_limits_same_session(tmp_path: Path, monkeypatch) -> None:
    codex_stop = load_codex_stop_module()
    codex_stop.DEDUP_FILE = tmp_path / ".last-codex-import.json"
    codex_stop.DEDUP_LOCK_FILE = tmp_path / ".locks" / "codex-stop.lock"
    codex_stop.MIN_SESSION_IMPORT_INTERVAL = 60

    monkeypatch.setattr(codex_stop.time, "time", lambda: 1_000)

    assert codex_stop.claim_import_key("session:a:turn:1", session_id="a") is True
    assert codex_stop.claim_import_key("session:a:turn:2", session_id="a") is False
    assert codex_stop.claim_import_key("session:b:turn:1", session_id="b") is True

    monkeypatch.setattr(codex_stop.time, "time", lambda: 1_061)

    assert codex_stop.claim_import_key("session:a:turn:3", session_id="a") is True
