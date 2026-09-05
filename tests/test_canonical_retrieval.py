from __future__ import annotations

import importlib.util
import io
import json
import sqlite3
from pathlib import Path

import pytest

import kb_db
import mcp_server
from memory_store import MemoryStore


def populated_store(root):
    store = MemoryStore(root)
    store.initialize()
    store.import_source("daily/archive/2026-04-01.md", "### Session\nCanonical sqlite backup provenance\n", archived=True)
    for name, projects in (("target", ["/repo/target"]), ("wrong", ["/repo/wrong"]), ("shared", [])):
        store.commit_articles([{"path": f"concepts/{name}", "summary": f"Canonical {name}", "projects": projects,
                                "body": f"---\ntitle: {name}\nsources: [daily/2026-04-01.md]\n"
                                        f"created: 2026-04-01\nupdated: 2026-09-05\n---\nCanonical sqlite {name}\n"}])
    return store


def test_canonical_mcp_reads_survive_stale_or_absent_exports(tmp_path, monkeypatch):
    store = populated_store(tmp_path)
    monkeypatch.setattr(mcp_server, "ROOT_DIR", tmp_path)
    knowledge = tmp_path / "knowledge"
    (knowledge / "concepts").mkdir(parents=True)
    (knowledge / "concepts" / "target.md").write_text("STALE", encoding="utf-8")
    monkeypatch.setattr(mcp_server, "KNOWLEDGE_DIR", knowledge)
    assert "Canonical sqlite target" in mcp_server.read_article("concepts/target")
    assert "Canonical sqlite shared" in mcp_server.read_article("concepts/shared")
    assert "Canonical target" in mcp_server.list_articles()
    assert "Canonical sqlite backup provenance" in mcp_server.read_source("daily/archive/2026-04-01.md")
    assert "provenance" in mcp_server.search_daily_logs("backup", last_n_days=0)
    assert store.usage_counts()["concepts/target"] == 1
    assert not (tmp_path / "scripts" / "usage.json").exists()


@pytest.mark.parametrize("path", ["concepts/target", "concepts/target.md"])
@pytest.mark.parametrize("symlink_directory", [False, True])
def test_canonical_read_ignores_export_symlinks(tmp_path, monkeypatch, path, symlink_directory):
    store = populated_store(tmp_path)
    knowledge = tmp_path / "knowledge"
    outside = tmp_path / "outside-export"
    outside.mkdir()
    (outside / "target.md").write_text("STALE EXPORT", encoding="utf-8")
    knowledge.mkdir()
    if symlink_directory:
        (knowledge / "concepts").symlink_to(outside, target_is_directory=True)
    else:
        (knowledge / "concepts").mkdir()
        (knowledge / "concepts" / "target.md").symlink_to(outside / "target.md")
    monkeypatch.setattr(mcp_server, "ROOT_DIR", tmp_path)
    monkeypatch.setattr(mcp_server, "KNOWLEDGE_DIR", knowledge)

    result = mcp_server.read_article(path)

    assert "Canonical sqlite target" in result
    assert "STALE EXPORT" not in result
    assert store.usage_counts()["concepts/target"] == 1


@pytest.mark.parametrize("path", ["", "concepts/", "concepts/target/child", "concepts/../target", "/concepts/target"])
def test_canonical_read_reports_invalid_identity_without_traceback(tmp_path, monkeypatch, path):
    store = populated_store(tmp_path)
    monkeypatch.setattr(mcp_server, "ROOT_DIR", tmp_path)
    monkeypatch.setattr(mcp_server, "KNOWLEDGE_DIR", tmp_path / "knowledge")

    assert mcp_server.read_article(path).startswith("Invalid article path:")
    assert store.usage_counts() == {}


def test_canonical_search_filters_project_before_limit_and_refreshes(tmp_path):
    store = populated_store(tmp_path)
    result = kb_db.search("sqlite", root=tmp_path, project="/repo/target/subdir", limit=1)
    assert [article["path"] for article in result] == ["concepts/target"]
    target = store.read_article("concepts/target")
    target["body"] = target["body"].replace("sqlite target", "postgres target")
    store.commit_articles([target])
    assert kb_db.search("sqlite", root=tmp_path, project="target") == []
    assert kb_db.search("postgres", root=tmp_path, project="target")[0]["path"] == "concepts/target"
    assert not (tmp_path / "scripts" / "kb-index.sqlite").exists()


def test_session_hook_reads_cwd_and_shared_canonical_rows(tmp_path, monkeypatch, capsys):
    populated_store(tmp_path)
    path = Path(__file__).parents[1] / "hooks" / "session-start.py"
    spec = importlib.util.spec_from_file_location("canonical_session_start", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    monkeypatch.setattr(mod, "ROOT", tmp_path)
    monkeypatch.setattr(mod.sys, "stdin", io.StringIO(json.dumps({"cwd": "/repo/target/src"})))
    monkeypatch.delenv("CLAUDE_INVOKED_BY", raising=False)
    monkeypatch.delenv("MEMORY_COMPILER_INTERNAL", raising=False)
    mod.main()
    context = json.loads(capsys.readouterr().out)["hookSpecificOutput"]["additionalContext"]
    assert "concepts/target" in context
    assert "concepts/shared" in context
    assert "concepts/wrong" not in context
    assert len(context.encode("utf-8")) <= 9500


def test_compile_slice_and_similarity_use_canonical_articles_without_sidecar(tmp_path, monkeypatch):
    populated_store(tmp_path)
    monkeypatch.setattr(kb_db, "KNOWLEDGE_DIR", tmp_path / "knowledge")
    monkeypatch.setattr(kb_db, "DB_FILE", tmp_path / "scripts" / "missing-index.sqlite")
    view = kb_db.compile_index_slice("sqlite")
    assert view is not None and "concepts/target" in view
    assert "3 of 3" in view
    pairs = kb_db.find_similar_pairs()
    assert pairs is not None and len(pairs) == 3


def test_explicit_fts_rebuild_uses_canonical_content(tmp_path, monkeypatch):
    populated_store(tmp_path)
    monkeypatch.setattr(kb_db, "KNOWLEDGE_DIR", tmp_path / "knowledge")
    assert kb_db.rebuild_index(tmp_path / "scripts" / "kb-index.sqlite") == 3


def test_corrupt_canonical_database_never_falls_back_to_markdown(tmp_path, monkeypatch):
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts" / "memory.sqlite").write_bytes(b"not a sqlite database")
    knowledge = tmp_path / "knowledge" / "concepts"
    knowledge.mkdir(parents=True)
    (knowledge / "stale.md").write_text("STALE", encoding="utf-8")
    monkeypatch.setattr(mcp_server, "ROOT_DIR", tmp_path)
    monkeypatch.setattr(mcp_server, "KNOWLEDGE_DIR", knowledge.parent)
    with pytest.raises((RuntimeError, sqlite3.Error)):
        mcp_server.read_article("concepts/stale")


def test_old_project_article_is_injected_even_without_hub_reads(tmp_path, monkeypatch):
    store = populated_store(tmp_path)
    target = store.read_article("concepts/target")
    target["body"] = target["body"].replace("updated: 2026-09-05", "updated: 2026-04-01")
    store.commit_articles([target])
    path = Path(__file__).parents[1] / "hooks" / "session-start.py"
    spec = importlib.util.spec_from_file_location("old_project_hook", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    monkeypatch.setattr(mod, "ROOT", tmp_path)
    assert "concepts/target" in mod.build_context("/repo/target/src")
