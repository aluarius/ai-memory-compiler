from __future__ import annotations

import json
from pathlib import Path

import codex_session


def test_build_codex_interactive_command_adds_cwd_by_default() -> None:
    cmd = codex_session.build_codex_interactive_command(
        cwd=Path("/repo"),
        forwarded_args=["--", "-m", "gpt-5.4", "continue"],
    )

    assert cmd[:3] == ["codex", "-C", "/repo"]
    assert cmd[-3:] == ["-m", "gpt-5.4", "continue"]


def test_build_codex_interactive_command_keeps_explicit_cd() -> None:
    cmd = codex_session.build_codex_interactive_command(
        cwd=Path("/repo"),
        forwarded_args=["--cd", "/tmp/other", "resume", "--last"],
    )

    assert cmd == ["codex", "--cd", "/tmp/other", "resume", "--last"]


def test_resolve_transcript_after_run_uses_history_fallback(tmp_path: Path) -> None:
    codex_home = tmp_path / ".codex"
    sessions_dir = codex_home / "sessions" / "2026" / "04" / "16"
    sessions_dir.mkdir(parents=True)
    session_index = codex_home / "session_index.jsonl"
    history = codex_home / "history.jsonl"
    cwd = Path("/repo")
    session_id = "019d-wrapper-session"
    transcript = sessions_dir / f"rollout-2026-04-16T12-00-00-{session_id}.jsonl"

    session_index.write_text(
        json.dumps({"id": session_id, "thread_name": "Wrapper test", "updated_at": "2026-04-16T12:00:00Z"}) + "\n",
        encoding="utf-8",
    )
    history.write_text(
        json.dumps({"session_id": session_id, "ts": 1_800_000_000, "text": "continue"}) + "\n",
        encoding="utf-8",
    )
    transcript.write_text(
        json.dumps(
            {
                "type": "session_meta",
                "payload": {"id": session_id, "cwd": str(cwd), "model_provider": "openai"},
            }
        )
        + "\n",
        encoding="utf-8",
    )

    resolved_id, resolved_path = codex_session.resolve_transcript_after_run(
        before_index={session_id: {"id": session_id, "updated_at": "2026-04-16T11:59:00Z"}},
        start_ts=1_799_999_999,
        cwd=cwd,
        session_index_file=session_index,
        history_file=history,
        sessions_dir=sessions_dir,
    )

    assert resolved_id == session_id
    assert resolved_path == transcript


def test_find_recent_transcripts_filters_by_cwd(tmp_path: Path) -> None:
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir()
    matching = sessions_dir / "rollout-a.jsonl"
    other = sessions_dir / "rollout-b.jsonl"

    matching.write_text(
        json.dumps({"type": "session_meta", "payload": {"id": "a", "cwd": "/repo"}}) + "\n",
        encoding="utf-8",
    )
    other.write_text(
        json.dumps({"type": "session_meta", "payload": {"id": "b", "cwd": "/other"}}) + "\n",
        encoding="utf-8",
    )

    resolved = codex_session.find_recent_transcripts(
        start_ts=0,
        cwd=Path("/repo"),
        sessions_dir=sessions_dir,
    )

    assert resolved == [matching]


def test_resolve_transcript_after_run_refuses_ambiguous_recent_fallback(tmp_path: Path) -> None:
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir()
    cwd = Path("/repo")

    first = sessions_dir / "rollout-a.jsonl"
    second = sessions_dir / "rollout-b.jsonl"
    for path, session_id in ((first, "a"), (second, "b")):
        path.write_text(
            json.dumps({"type": "session_meta", "payload": {"id": session_id, "cwd": str(cwd)}}) + "\n",
            encoding="utf-8",
        )

    resolved_id, resolved_path = codex_session.resolve_transcript_after_run(
        before_index={},
        start_ts=0,
        cwd=cwd,
        session_index_file=tmp_path / "session_index.jsonl",
        history_file=tmp_path / "history.jsonl",
        sessions_dir=sessions_dir,
    )

    assert resolved_id is None
    assert resolved_path is None
