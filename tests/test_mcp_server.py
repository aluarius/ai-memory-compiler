from __future__ import annotations

import json
from pathlib import Path

import mcp_server


def _setup(monkeypatch, tmp_path: Path) -> Path:
    knowledge_dir = tmp_path / "knowledge"
    concepts = knowledge_dir / "concepts"
    concepts.mkdir(parents=True)
    monkeypatch.setattr(mcp_server, "KNOWLEDGE_DIR", knowledge_dir)
    monkeypatch.setattr(mcp_server, "ARTICLE_DIRS", [concepts])
    monkeypatch.setattr(mcp_server, "USAGE_FILE", tmp_path / "usage.json")
    monkeypatch.setattr(mcp_server, "USAGE_LOCK", tmp_path / "usage.lock")
    # default to no FTS index so legacy-path tests never touch the real DB;
    # FTS tests re-patch kb_db.search explicitly
    monkeypatch.setattr(mcp_server.kb_db, "search", lambda query, limit=10: None)
    return concepts


def test_search_ranks_title_match_above_body_mention(monkeypatch, tmp_path: Path) -> None:
    concepts = _setup(monkeypatch, tmp_path)
    (concepts / "docker-guide.md").write_text(
        '---\ntitle: "Docker Guide"\n---\n\nAbout docker.\n', encoding="utf-8"
    )
    (concepts / "misc.md").write_text(
        '---\ntitle: "Misc"\n---\n\nMentions docker once.\n', encoding="utf-8"
    )

    result = mcp_server.search_knowledge("docker")

    assert result.index("docker-guide") < result.index("misc.md")


def test_search_skips_unmatched_articles(monkeypatch, tmp_path: Path) -> None:
    concepts = _setup(monkeypatch, tmp_path)
    (concepts / "unrelated.md").write_text(
        '---\ntitle: "Unrelated"\n---\n\nNothing here.\n', encoding="utf-8"
    )

    result = mcp_server.search_knowledge("docker")

    assert "No articles matching" in result


def test_read_article_records_usage(monkeypatch, tmp_path: Path) -> None:
    concepts = _setup(monkeypatch, tmp_path)
    (concepts / "topic.md").write_text("---\ntitle: T\n---\n\nBody.\n", encoding="utf-8")

    mcp_server.read_article("concepts/topic")
    mcp_server.read_article("concepts/topic.md")

    data = json.loads((tmp_path / "usage.json").read_text(encoding="utf-8"))
    assert data["article_reads"]["concepts/topic"]["count"] == 2


def test_usage_recording_survives_corrupt_file(monkeypatch, tmp_path: Path) -> None:
    concepts = _setup(monkeypatch, tmp_path)
    (concepts / "topic.md").write_text("Body.", encoding="utf-8")
    (tmp_path / "usage.json").write_text("{corrupt", encoding="utf-8")

    assert "Body." in mcp_server.read_article("concepts/topic")
    data = json.loads((tmp_path / "usage.json").read_text(encoding="utf-8"))
    assert data["article_reads"]["concepts/topic"]["count"] == 1


# ---------------------------------------------------------------------------
# FTS-backed search with legacy fallback
# ---------------------------------------------------------------------------


def test_search_uses_fts_results_when_db_available(monkeypatch, tmp_path: Path) -> None:
    _setup(monkeypatch, tmp_path)
    monkeypatch.setattr(
        mcp_server.kb_db, "search",
        lambda query, limit=10: [{
            "path": "concepts/docker-deploy", "title": "Docker Deploy",
            "summary": "s", "updated": "2026-07-01", "snippet": "«nginx» recreate",
        }],
    )

    result = mcp_server.search_knowledge("nginx")

    assert "concepts/docker-deploy" in result
    assert "«nginx» recreate" in result


def test_search_falls_back_to_scan_when_db_missing(monkeypatch, tmp_path: Path) -> None:
    concepts = _setup(monkeypatch, tmp_path)
    (concepts / "topic.md").write_text(
        '---\ntitle: "Topic"\n---\n\nnginx mentioned here.\n', encoding="utf-8"
    )
    monkeypatch.setattr(mcp_server.kb_db, "search", lambda query, limit=10: None)

    result = mcp_server.search_knowledge("nginx")

    assert "concepts/topic.md" in result  # legacy scan formatting


def test_search_fts_empty_reports_no_match(monkeypatch, tmp_path: Path) -> None:
    _setup(monkeypatch, tmp_path)
    monkeypatch.setattr(mcp_server.kb_db, "search", lambda query, limit=10: [])

    assert "No articles matching" in mcp_server.search_knowledge("zzz")
