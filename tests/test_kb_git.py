from __future__ import annotations

from pathlib import Path

import kb_git


def _setup_kb(monkeypatch, tmp_path: Path) -> Path:
    knowledge_dir = tmp_path / "knowledge"
    knowledge_dir.mkdir()
    (knowledge_dir / "index.md").write_text("# Index\n", encoding="utf-8")
    monkeypatch.setattr(kb_git, "KNOWLEDGE_DIR", knowledge_dir)
    monkeypatch.setattr(kb_git, "INFLIGHT_FILE", tmp_path / "compile-inflight.json")
    return knowledge_dir


def test_ensure_kb_repo_initializes_and_commits(monkeypatch, tmp_path: Path) -> None:
    kb = _setup_kb(monkeypatch, tmp_path)
    assert kb_git.ensure_kb_repo() is True
    assert (kb / ".git").exists()
    assert kb_git.kb_is_dirty() is False
    assert kb_git.ensure_kb_repo() is False  # idempotent


def test_kb_commit_and_dirty_cycle(monkeypatch, tmp_path: Path) -> None:
    kb = _setup_kb(monkeypatch, tmp_path)
    kb_git.ensure_kb_repo()
    (kb / "concepts").mkdir()
    (kb / "concepts" / "new.md").write_text("body", encoding="utf-8")
    assert kb_git.kb_is_dirty() is True
    assert kb_git.kb_commit("compile test") is True
    assert kb_git.kb_is_dirty() is False
    assert kb_git.kb_commit("nothing to do") is False


def test_kb_rollback_discards_tracked_and_untracked(monkeypatch, tmp_path: Path) -> None:
    kb = _setup_kb(monkeypatch, tmp_path)
    kb_git.ensure_kb_repo()
    (kb / "index.md").write_text("# Index\nmutated\n", encoding="utf-8")
    (kb / "partial.md").write_text("partial write", encoding="utf-8")
    assert kb_git.kb_rollback() is True
    assert (kb / "index.md").read_text(encoding="utf-8") == "# Index\n"
    assert not (kb / "partial.md").exists()


def test_inflight_marker_roundtrip(monkeypatch, tmp_path: Path) -> None:
    _setup_kb(monkeypatch, tmp_path)
    assert kb_git.read_inflight() is None
    kb_git.mark_inflight("2026-07-01.md")
    assert kb_git.read_inflight() == "2026-07-01.md"
    kb_git.clear_inflight()
    assert kb_git.read_inflight() is None


def test_recover_interrupted_compile_requires_marker(monkeypatch, tmp_path: Path) -> None:
    kb = _setup_kb(monkeypatch, tmp_path)
    kb_git.ensure_kb_repo()
    (kb / "partial.md").write_text("partial", encoding="utf-8")

    assert kb_git.recover_interrupted_compile() is False  # no marker: keep changes
    assert (kb / "partial.md").exists()

    kb_git.mark_inflight("2026-07-01.md")
    assert kb_git.recover_interrupted_compile() is True
    assert not (kb / "partial.md").exists()
    assert kb_git.read_inflight() is None


def test_kb_functions_noop_without_repo(monkeypatch, tmp_path: Path) -> None:
    _setup_kb(monkeypatch, tmp_path)  # no ensure_kb_repo -> no .git
    assert kb_git.kb_is_dirty() is False
    assert kb_git.kb_commit("x") is False
    assert kb_git.kb_rollback() is False
