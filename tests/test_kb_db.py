from __future__ import annotations

from pathlib import Path

import kb_db


HEADER = "# Index\n\n| Article | Summary | Compiled From | Updated |\n|---|---|---|---|\n"


def _setup_kb(monkeypatch, tmp_path: Path) -> Path:
    knowledge_dir = tmp_path / "knowledge"
    concepts = knowledge_dir / "concepts"
    concepts.mkdir(parents=True)

    (concepts / "docker-deploy.md").write_text(
        '---\ntitle: "Docker Deploy Patterns"\n---\n\n'
        "Force-recreate nginx after rsync; bind mounts pin old inodes.\n",
        encoding="utf-8",
    )
    (concepts / "vue-refactor.md").write_text(
        '---\ntitle: "Vue Refactor"\n---\n\n'
        "Extract visual subtrees. A docker aside mentioned once.\n",
        encoding="utf-8",
    )
    (concepts / "russian-topic.md").write_text(
        '---\ntitle: "Боевой рантайм"\n---\n\n'
        "Регенерация хитов в бою завязана на battle_users.\n",
        encoding="utf-8",
    )
    (knowledge_dir / "index.md").write_text(HEADER + "\n".join([
        "| [[concepts/docker-deploy]] | nginx recreate and stale inodes | daily/a.md, daily/b.md, daily/c.md | 2026-07-01 |",
        "| [[concepts/vue-refactor]] | vue subtree extraction | daily/b.md | 2026-07-16 |",
        "| [[concepts/russian-topic]] | регенерация в бою | daily/c.md | 2026-06-01 |",
    ]) + "\n", encoding="utf-8")

    monkeypatch.setattr(kb_db, "KNOWLEDGE_DIR", knowledge_dir)
    monkeypatch.setattr(kb_db, "INDEX_FILE", knowledge_dir / "index.md")
    monkeypatch.setattr(
        kb_db, "list_wiki_articles", lambda: sorted(concepts.glob("*.md"))
    )
    return tmp_path / "kb-index.sqlite"


def test_rebuild_and_search_roundtrip(monkeypatch, tmp_path: Path) -> None:
    db = _setup_kb(monkeypatch, tmp_path)

    count = kb_db.rebuild_index(db_path=db)
    assert count == 3

    results = kb_db.search("nginx rsync", db_path=db)
    assert results and results[0]["path"] == "concepts/docker-deploy"
    assert results[0]["title"] == "Docker Deploy Patterns"
    assert results[0]["updated"] == "2026-07-01"
    assert "nginx" in results[0]["snippet"].lower()


def test_search_supports_russian(monkeypatch, tmp_path: Path) -> None:
    db = _setup_kb(monkeypatch, tmp_path)
    kb_db.rebuild_index(db_path=db)

    results = kb_db.search("регенерация", db_path=db)

    assert results and results[0]["path"] == "concepts/russian-topic"


def test_search_ranks_title_match_above_body_mention(monkeypatch, tmp_path: Path) -> None:
    db = _setup_kb(monkeypatch, tmp_path)
    kb_db.rebuild_index(db_path=db)

    results = kb_db.search("docker", db_path=db)

    paths = [r["path"] for r in results]
    assert paths.index("concepts/docker-deploy") < paths.index("concepts/vue-refactor")


def test_search_sanitizes_punctuation(monkeypatch, tmp_path: Path) -> None:
    db = _setup_kb(monkeypatch, tmp_path)
    kb_db.rebuild_index(db_path=db)

    # raw FTS5 would raise on unbalanced quotes / operators
    results = kb_db.search('what\'s "nginx" AND (rsync?', db_path=db)
    assert results and results[0]["path"] == "concepts/docker-deploy"

    assert kb_db.search("§ №», ...", db_path=db) == []


def test_search_returns_none_without_db(tmp_path: Path) -> None:
    assert kb_db.search("anything", db_path=tmp_path / "missing.sqlite") is None


def test_rebuild_is_idempotent(monkeypatch, tmp_path: Path) -> None:
    db = _setup_kb(monkeypatch, tmp_path)
    kb_db.rebuild_index(db_path=db)
    count = kb_db.rebuild_index(db_path=db)

    assert count == 3
    assert len(kb_db.search("docker", db_path=db)) == 2  # no duplicate rows


def test_compile_index_slice_recent_candidates_and_pointer(monkeypatch, tmp_path: Path) -> None:
    db = _setup_kb(monkeypatch, tmp_path)
    kb_db.rebuild_index(db_path=db)
    monkeypatch.setattr(kb_db, "_today", lambda: "2026-07-17")

    log_text = "Session about nginx deploy: rsync recreate, stale inode pinning."
    view = kb_db.compile_index_slice(log_text, db_path=db)

    assert view is not None
    # recent row (updated within window)
    assert "concepts/vue-refactor" in view
    # FTS candidate from the log text despite an old updated date
    assert "concepts/docker-deploy" in view
    # pointer + anti-duplication instruction
    assert "index.md" in view
    assert "grep" in view.lower() or "Grep" in view


def test_compile_index_slice_none_without_db(monkeypatch, tmp_path: Path) -> None:
    _setup_kb(monkeypatch, tmp_path)

    assert kb_db.compile_index_slice("text", db_path=tmp_path / "missing.sqlite") is None
