from __future__ import annotations

import hashlib

import pytest


def article(path, body, projects=None):
    return {"path": path, "title": path, "summary": "", "body": body, "updated": "2026-09-05",
            "sources": [], "projects": projects or [], "revision": 1,
            "content_hash": hashlib.sha256(body.encode()).hexdigest()}


class TinyEmbedder:
    """Deterministic external embedding boundary; storage/ranking remain real."""

    def embed_documents(self, texts):
        return [[1.0, 0.0] if "backup" in text else [0.0, 1.0] for text in texts]

    def embed_query(self, text):
        return [1.0, 0.0]


def test_semantic_search_restores_cross_language_answer_and_detects_staleness(tmp_path):
    from semantic_search import SemanticIndex, SemanticUnavailable, hybrid_search
    records = [article("concepts/backup", "sqlite backup"), article("concepts/vue", "vue setup")]
    index = SemanticIndex(tmp_path, backend=TinyEmbedder())
    assert index.rebuild(records) == 2
    assert index.search("как сохранить базу", records)[0]["path"] == "concepts/backup"
    assert hybrid_search("как сохранить базу", records, root=tmp_path, index=index)[0]["path"] == "concepts/backup"
    records[0] = article("concepts/backup", "changed sqlite backup")
    with pytest.raises(SemanticUnavailable, match="stale"):
        index.search("как сохранить базу", records)


def test_missing_index_does_not_load_or_download_model(tmp_path, monkeypatch):
    import semantic_search
    def forbidden(*args, **kwargs):
        pytest.fail("A query without an index must not load a model")
    monkeypatch.setattr(semantic_search, "FastEmbedBackend", forbidden)
    with pytest.raises(semantic_search.SemanticUnavailable, match="index"):
        semantic_search.SemanticIndex(tmp_path).search("query", [article("concepts/a", "a")])
    assert not (tmp_path / "scripts").exists()


def test_semantic_model_and_summary_changes_invalidate_vectors(tmp_path):
    from semantic_search import SemanticIndex, SemanticUnavailable
    records = [article("concepts/backup", "sqlite backup")]
    index = SemanticIndex(tmp_path, backend=TinyEmbedder())
    index.rebuild(records)
    with pytest.raises(SemanticUnavailable, match="model"):
        SemanticIndex(tmp_path, backend=TinyEmbedder(), model="different").search("query", records)
    records[0]["summary"] = "a changed summary"
    with pytest.raises(SemanticUnavailable, match="stale"):
        index.search("query", records)


def test_project_filter_applies_before_top_k(tmp_path):
    from kb_db import search_records
    from semantic_search import SemanticIndex
    records = [article(f"concepts/wrong-{i}", "sqlite backup", ["wrong"]) for i in range(20)]
    target = article("concepts/right", "sqlite backup", ["right"])
    records.append(target)
    index = SemanticIndex(tmp_path, backend=TinyEmbedder())
    index.rebuild(records)
    assert index.search("query", [target], limit=1)[0]["path"] == "concepts/right"
    assert search_records("sqlite", [target], limit=1)[0]["path"] == "concepts/right"


def test_failed_reindex_keeps_previous_complete_index(tmp_path):
    from semantic_search import SemanticIndex
    records = [article("concepts/backup", "sqlite backup")]
    index = SemanticIndex(tmp_path, backend=TinyEmbedder())
    index.rebuild(records)
    class FailingEmbedder(TinyEmbedder):
        def embed_documents(self, texts):
            raise RuntimeError("embedding interrupted")
    with pytest.raises(RuntimeError, match="interrupted"):
        SemanticIndex(tmp_path, backend=FailingEmbedder()).rebuild(records + [article("concepts/new", "new")])
    assert index.search("query", records)[0]["path"] == "concepts/backup"


def test_reindex_reuses_unchanged_content_without_loading_model(tmp_path):
    from semantic_search import SemanticIndex
    records = [article("concepts/backup", "sqlite backup")]
    index = SemanticIndex(tmp_path, backend=TinyEmbedder())
    index.rebuild(records)
    class UnavailableEmbedder(TinyEmbedder):
        def embed_documents(self, texts):
            raise RuntimeError("model is unavailable")
    assert SemanticIndex(tmp_path, backend=UnavailableEmbedder()).rebuild(records) == 1
    assert index.search("query", records)[0]["path"] == "concepts/backup"
