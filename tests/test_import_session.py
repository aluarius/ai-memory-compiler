from __future__ import annotations

import argparse
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
    monkeypatch.setattr(
        import_session.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(returncode=0),
    )

    assert import_session.main() == 0

    contexts = list(tmp_path.glob("import-flush-*.md"))
    assert len(contexts) == 1
    context = contexts[0].read_text(encoding="utf-8")
    assert "Can we reuse the same memory pipeline for Codex?" not in context
    assert "Import the transcript into the shared flush and compile flow." in context
