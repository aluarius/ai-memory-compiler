from __future__ import annotations

import argparse
import sys
from pathlib import Path
from types import SimpleNamespace

import import_session


FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"


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
