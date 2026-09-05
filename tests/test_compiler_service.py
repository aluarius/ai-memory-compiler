from __future__ import annotations

import asyncio
import hashlib
import json
import sqlite3
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from memory_store import MemoryStore, StoreConflict, StoreValidationError


SOURCE = "daily/2026-09-01.md"


def article(name: str = "topic", links: str = "", summary: str = "A useful summary") -> dict:
    return {
        "path": f"concepts/{name}", "summary": summary, "projects": ["example"],
        "body": f"""---
title: {name}
sources: [daily/2026-09-01.md]
created: 2026-09-01
updated: 2026-09-01
---

# {name}

Useful facts. {links}

## Sources

- [[daily/2026-09-01]]
""",
    }


@pytest.fixture
def store(tmp_path: Path) -> MemoryStore:
    result = MemoryStore(tmp_path)
    result.initialize()
    result.import_source(SOURCE, "# Daily log\n\n### Session one\nUseful knowledge.\n")
    return result


def test_commit_refreshes_opted_in_semantics_without_download(store, monkeypatch):
    import compiler_service
    import semantic_search
    calls = []
    (store.root / "scripts/semantic-index.sqlite").touch()
    monkeypatch.setattr(semantic_search.SemanticIndex, "rebuild", lambda self, rows, **kw: calls.append(kw))
    assert compiler_service.export_committed(store) is None
    assert calls == [{"allow_download": False}]


def install_model(monkeypatch, response: str, inspect=None):
    import compiler_service

    async def fake(prompt: str, **kwargs):
        if inspect:
            inspect(prompt, kwargs)
        return SimpleNamespace(text=response, cost_usd=0.25, runtime="fixture", model="fixture")

    monkeypatch.setattr(compiler_service, "call_readonly_model", fake)
    monkeypatch.setattr(compiler_service, "export_committed", lambda store: None)
    return compiler_service


@pytest.mark.parametrize("response", [
    "not JSON", '{"articles":', '{"articles": []}',
    '{"articles": [], "no_changes_reason": ""}',
    '{"articles": [], "articles": [], "no_changes_reason": "duplicate keys"}',
    '{"articles": [], "no_changes_reason": "okay", "unexpected": true}',
])
def test_invalid_model_output_preserves_checkpoint_and_generation(store, monkeypatch, response):
    service = install_model(monkeypatch, response)
    before = store.snapshot()

    with pytest.raises(StoreValidationError):
        asyncio.run(service.compile_source(store, SOURCE))

    assert store.snapshot() == before


def test_broken_links_roll_back_articles_checkpoint_and_log(store, monkeypatch):
    service = install_model(monkeypatch, json.dumps({
        "articles": [article("new", "[[concepts/missing]]")],
    }))

    with pytest.raises(StoreValidationError, match="missing"):
        asyncio.run(service.compile_source(store, SOURCE))

    assert store.list_articles() == []
    assert store.get_state("pipeline", {}) == {}
    assert store.get_state("build_log", "") == ""


def test_stale_generation_never_overwrites_newer_articles_or_checkpoint(store, monkeypatch):
    def concurrent_change(prompt, kwargs):
        store.commit_articles([article("concurrent")])

    service = install_model(monkeypatch, json.dumps({"articles": [article()]}), concurrent_change)

    with pytest.raises(StoreConflict):
        asyncio.run(service.compile_source(store, SOURCE))

    assert [row["path"] for row in store.list_articles()] == ["concepts/concurrent"]
    assert store.get_state("pipeline", {}) == {}


def test_success_uses_canonical_snapshot_and_commits_checkpoint_after_model(store, monkeypatch):
    previous = article("existing")
    store.commit_articles([previous])
    seen = []

    def inspect(prompt, kwargs):
        cwd = kwargs["cwd"]
        seen.append(cwd)
        assert cwd != store.root
        assert (cwd / "knowledge/concepts/existing.md").read_text() == previous["body"]
        assert (cwd / SOURCE).read_text() == store.read_source(SOURCE)
        assert "JSON" in prompt
        with sqlite3.connect(store.db_path, timeout=0) as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.rollback()

    service = install_model(monkeypatch, json.dumps({"articles": [article()]}), inspect)
    result = asyncio.run(service.compile_source(store, SOURCE))

    assert result["changed"] == 1
    assert result["cost_usd"] == 0.25
    assert store.read_article("concepts/topic")["revision"] == 1
    entry = store.get_state("pipeline")["ingested"]["2026-09-01.md"]
    raw = store.read_source(SOURCE).encode()
    assert entry["hash"] == hashlib.sha256(raw).hexdigest()
    assert entry["size"] == len(raw)
    assert "compile" in store.get_state("build_log")
    assert not seen[0].exists()


def test_source_append_during_model_does_not_advance_past_processed_snapshot(store, monkeypatch):
    original = store.read_source(SOURCE)
    service = install_model(
        monkeypatch, '{"articles": [], "no_changes_reason": "No durable facts"}',
        lambda *_: store.append_source(SOURCE, "Later session\n", event_key="later"),
    )

    asyncio.run(service.compile_source(store, SOURCE))

    checkpoint = store.get_state("pipeline")["ingested"]["2026-09-01.md"]
    assert checkpoint["size"] == len(original.encode())
    assert checkpoint["hash"] == hashlib.sha256(original.encode()).hexdigest()
    assert len(service.pending_sources(store.snapshot())) == 1


@pytest.mark.parametrize("summary", ["A useful summary", "x" * 250])
def test_exact_edits_commit_complete_revision_and_preserve_omitted_metadata(store, monkeypatch, summary):
    original = article(summary=summary)
    store.commit_articles([original])
    service = install_model(monkeypatch, json.dumps({"articles": [{
        "path": "concepts/topic", "edits": [
            {"old": "Useful facts.", "new": "Useful facts. First."},
            {"old": "First.", "new": "First. Second."},
        ],
    }]}))

    result = asyncio.run(service.compile_source(store, SOURCE))

    saved = store.read_article("concepts/topic")
    assert saved["body"] == original["body"].replace("Useful facts.", "Useful facts. First. Second.")
    assert saved["summary"] == summary
    assert saved["projects"] == ["example"]
    assert saved["revision"] == 2
    assert store.revisions("concepts/topic")[0]["body"] == original["body"]
    assert result["processed_bytes"] == len(store.read_source(SOURCE).encode())


@pytest.mark.parametrize("change", [
    {"path": "concepts/topic", "edits": [{"old": "Absent text", "new": "Changed"}]},
    {"path": "concepts/topic", "edits": [{"old": "topic", "new": "Changed"}]},
    {"path": "concepts/topic", "edits": [{"old": "", "new": "Changed"}]},
    {"path": "concepts/topic", "edits": [{"old": "Useful facts.", "new": None}]},
    {"path": "concepts/topic", "edits": [{"old": "Useful facts.", "new": "Changed", "all": True}]},
    {"path": "concepts/topic", "edits": {"old": "Useful facts.", "new": "Changed"}},
    {"path": "concepts/topic", "edits": []},
    {"path": "concepts/unknown", "edits": [{"old": "Useful facts.", "new": "Changed"}]},
    {"path": None, "edits": [{"old": "Useful facts.", "new": "Changed"}]},
    {"path": 42, "edits": [{"old": "Useful facts.", "new": "Changed"}]},
    {"path": "concepts/topic", "body": "Conflicting replacement", "edits": []},
])
def test_invalid_exact_edits_preserve_entire_snapshot(store, monkeypatch, change):
    store.commit_articles([article()])
    before = store.snapshot()
    service = install_model(monkeypatch, json.dumps({"articles": [change]}))

    with pytest.raises(StoreValidationError, match="(?i)edit"):
        asyncio.run(service.compile_source(store, SOURCE))

    assert store.snapshot() == before


def test_overlapping_exact_matches_are_ambiguous(store, monkeypatch):
    original = article()
    original["body"] += "\nbanana\n"
    store.commit_articles([original])
    before = store.snapshot()
    service = install_model(monkeypatch, json.dumps({"articles": [{
        "path": "concepts/topic", "edits": [{"old": "ana", "new": "ambiguous"}],
    }]}))

    with pytest.raises(StoreValidationError, match="match once"):
        asyncio.run(service.compile_source(store, SOURCE))

    assert store.snapshot() == before


def test_exact_edits_cannot_bypass_final_graph_validation(store, monkeypatch):
    store.commit_articles([article()])
    before = store.snapshot()
    service = install_model(monkeypatch, json.dumps({"articles": [{
        "path": "concepts/topic", "edits": [{"old": "Useful facts.", "new": "[[concepts/missing]]"}],
    }]}))

    with pytest.raises(StoreValidationError, match="missing"):
        asyncio.run(service.compile_source(store, SOURCE))

    assert store.snapshot() == before


def test_exact_edits_use_original_generation_not_newer_database_contents(store, monkeypatch):
    store.commit_articles([article()])
    service = install_model(monkeypatch, json.dumps({"articles": [{
        "path": "concepts/topic", "edits": [{"old": "Useful facts.", "new": "Stale proposal."}],
    }]}), lambda *_: store.commit_articles([article("concurrent")]))

    with pytest.raises(StoreConflict):
        asyncio.run(service.compile_source(store, SOURCE))

    assert "Stale proposal." not in store.read_article("concepts/topic")["body"]
    assert "ingested" not in store.get_state("pipeline", {})


def test_summary_only_edits_do_not_require_repeating_article_body(store, monkeypatch):
    original = article(summary="x" * 250)
    store.commit_articles([original])
    service = install_model(monkeypatch, json.dumps({"articles": [{
        "path": "concepts/topic", "edits": [], "summary": "Concise summary",
    }]}))

    assert asyncio.run(service.rewrite_summaries(store)) == 1
    saved = store.read_article("concepts/topic")
    assert saved["body"] == original["body"]
    assert saved["projects"] == ["example"]
    assert saved["summary"] == "Concise summary"


def test_consolidation_materializes_edits_before_provenance_validation(store, monkeypatch):
    store.commit_articles([article("a"), article("b")])
    service = install_model(monkeypatch, json.dumps({
        "articles": [{"path": "concepts/a", "edits": [{"old": "Useful facts.", "new": "Combined facts."}]}],
        "deletions": ["concepts/b"],
    }))

    assert asyncio.run(service.consolidate_articles(store, [{"a": "concepts/a", "b": "concepts/b"}]))
    assert store.read_article("concepts/b") is None
    assert "Combined facts." in store.read_article("concepts/a")["body"]


def test_bounded_batches_preserve_utf8_and_resume_from_legacy_prefix_hash(store, monkeypatch):
    original = store.read_source(SOURCE)
    text = original + "Сеанс.\n" * 20
    store.import_source(SOURCE, text)
    store.set_state("pipeline", {"ingested": {"2026-09-01.md": {
        "size": len(original.encode()), "hash": hashlib.sha256(original.encode()).hexdigest()[:16],
    }}})
    seen = []
    service = install_model(
        monkeypatch, '{"articles": [], "no_changes_reason": "No durable facts"}',
        lambda _, kwargs: seen.append((kwargs["cwd"] / SOURCE).read_bytes()),
    )

    first = asyncio.run(service.compile_source(store, SOURCE, max_batch_bytes=25))
    second = asyncio.run(service.compile_source(store, SOURCE, max_batch_bytes=25))

    assert all(0 < len(part) <= 25 for part in seen)
    assert first["remaining_bytes"] > second["remaining_bytes"] > 0
    checkpoint = store.get_state("pipeline")["ingested"]["2026-09-01.md"]
    prefix = original.encode() + b"".join(seen)
    assert checkpoint["size"] == len(prefix)
    assert checkpoint["hash"] == hashlib.sha256(prefix).hexdigest()


def test_export_failure_keeps_successful_commit_without_recompiling(store, monkeypatch):
    service = install_model(monkeypatch, json.dumps({"articles": [article()]}))
    monkeypatch.setattr(service, "export_committed", lambda store: "export blocked")

    result = asyncio.run(service.compile_source(store, SOURCE))

    assert result["export_error"] == "export blocked"
    assert store.read_article("concepts/topic") is not None
    assert service.pending_sources(store.snapshot()) == []


def test_summary_rewrite_changes_only_summary_and_uses_generation_guard(store, monkeypatch):
    original = article(summary="x" * 250)
    store.commit_articles([original])
    update = {**original, "summary": "Clear summary"}
    service = install_model(monkeypatch, json.dumps({"articles": [update]}))

    assert asyncio.run(service.rewrite_summaries(store)) == 1
    saved = store.read_article(original["path"])
    assert saved["body"] == original["body"]
    assert saved["summary"] == "Clear summary"

    store.commit_articles([original])
    install_model(monkeypatch, json.dumps({"articles": [update]}),
                  lambda *_: store.commit_articles([article("concurrent")]))
    with pytest.raises(StoreConflict):
        asyncio.run(service.rewrite_summaries(store))
    assert store.read_article(original["path"])["summary"] == original["summary"]


def test_summary_rewrite_cannot_modify_article_body(store, monkeypatch):
    original = article(summary="x" * 250)
    store.commit_articles([original])
    update = {**original, "summary": "Clear summary", "body": original["body"] + "Changed"}
    service = install_model(monkeypatch, json.dumps({"articles": [update]}))

    with pytest.raises(StoreValidationError):
        asyncio.run(service.rewrite_summaries(store))
    assert store.read_article(original["path"])["body"] == original["body"]


def test_consolidation_folds_atomically_and_rejects_dangling_links(store, monkeypatch):
    store.commit_articles([article("a", "[[concepts/b]]"), article("b")])
    pairs = [{"a": "concepts/a", "b": "concepts/b"}]
    service = install_model(monkeypatch, json.dumps({
        "articles": [article("a", "[[concepts/b]]")], "deletions": ["concepts/b"],
    }))

    with pytest.raises(StoreValidationError, match="missing"):
        asyncio.run(service.consolidate_articles(store, pairs))
    assert store.read_article("concepts/b") is not None

    install_model(monkeypatch, json.dumps({"articles": [article("a")], "deletions": ["concepts/b"]}))
    assert asyncio.run(service.consolidate_articles(store, pairs)) is True
    assert store.read_article("concepts/b") is None
    assert store.read_article("concepts/a")["revision"] == 2


@pytest.mark.parametrize("deletions", [["concepts/a", "concepts/b"], ["concepts/b"]])
def test_consolidation_cannot_delete_facts_without_a_surviving_replacement(store, monkeypatch, deletions):
    store.commit_articles([article("a"), article("b")])
    service = install_model(monkeypatch, json.dumps({"articles": [], "deletions": deletions}))

    with pytest.raises(StoreValidationError):
        asyncio.run(service.consolidate_articles(store, [{"a": "concepts/a", "b": "concepts/b"}]))
    assert len(store.list_articles()) == 2


def test_compile_specific_source_resumes_a_successful_prefix(store, monkeypatch):
    original = store.read_source(SOURCE)
    store.set_state("pipeline", {"ingested": {"2026-09-01.md": {
        "size": len(original.encode()), "hash": hashlib.sha256(original.encode()).hexdigest(),
    }}})
    store.append_source(SOURCE, "New session\n", event_key="new")
    seen = []
    service = install_model(
        monkeypatch, '{"articles": [], "no_changes_reason": "No durable facts"}',
        lambda _, kwargs: seen.append((kwargs["cwd"] / SOURCE).read_text()),
    )

    assert asyncio.run(service.run_compile(store, source=SOURCE)) == 0
    assert seen == ["New session\n"]


def test_consolidation_must_preserve_deleted_article_sources(store, monkeypatch):
    store.import_source("daily/2026-09-02.md", "Another source")
    other = article("b")
    other["body"] = other["body"].replace("2026-09-01", "2026-09-02")
    store.commit_articles([article("a"), other])
    service = install_model(monkeypatch, json.dumps({
        "articles": [article("a")], "deletions": ["concepts/b"],
    }))

    with pytest.raises(StoreValidationError, match="sources"):
        asyncio.run(service.consolidate_articles(store, [{"a": "concepts/a", "b": "concepts/b"}]))
    assert store.read_article("concepts/b") is not None


def test_compile_cli_uses_database_without_source_export_or_git(store, monkeypatch):
    import compile as compile_script

    install_model(monkeypatch, json.dumps({"articles": [article()]}))
    monkeypatch.setattr(compile_script, "ROOT_DIR", store.root)
    monkeypatch.setattr(compile_script, "LOCKS_DIR", store.root / "scripts/.locks")
    monkeypatch.setattr(compile_script, "ensure_kb_repo", lambda: pytest.fail("canonical Git access"))
    monkeypatch.setattr(sys, "argv", ["compile.py", "--file", SOURCE])

    compile_script.main()

    assert store.read_article("concepts/topic") is not None


def test_index_rewrite_entrypoint_reads_database_without_exports_or_git(store, monkeypatch):
    import index_rewrite

    original = article(summary="x" * 250)
    store.commit_articles([original])
    install_model(monkeypatch, json.dumps({"articles": [{**original, "summary": "Clear summary"}]}))
    monkeypatch.setattr(index_rewrite, "KNOWLEDGE_DIR", store.root / "knowledge")
    monkeypatch.setattr(index_rewrite, "INDEX_FILE", store.root / "knowledge/index.md")
    monkeypatch.setattr(sys, "argv", ["index_rewrite.py"])

    assert index_rewrite.main() == 0
    assert store.read_article(original["path"])["summary"] == "Clear summary"


def test_consolidation_entrypoint_reads_database_without_exports_or_git(store, monkeypatch):
    import consolidate

    store.commit_articles([article("a", "[[concepts/b]]"), article("b")])
    install_model(monkeypatch, json.dumps({"articles": [article("a")], "deletions": ["concepts/b"]}))
    monkeypatch.setattr(consolidate, "KNOWLEDGE_DIR", store.root / "knowledge")
    monkeypatch.setattr(consolidate.kb_db, "find_similar_pairs", lambda limit: [
        {"a": "concepts/a", "b": "concepts/b", "score": 1.0, "linked": True},
    ])
    monkeypatch.setattr(consolidate, "ensure_kb_repo", lambda: pytest.fail("canonical Git access"))
    monkeypatch.setattr(consolidate, "update_state", lambda _: pytest.fail("legacy state write"))

    assert asyncio.run(consolidate.run_consolidation()) is True
    assert store.read_article("concepts/b") is None


def test_run_compile_records_complete_status_without_losing_pipeline_state(store, monkeypatch):
    service = install_model(monkeypatch, json.dumps({"articles": [article()]}))
    store.set_state("pipeline", {"unrelated": "preserved", "last_compile": {"status": "failed"}})

    assert asyncio.run(service.run_compile(store)) == 0

    pipeline = store.get_state("pipeline")
    assert pipeline["last_compile"]["status"] == "complete"
    assert SOURCE in pipeline["last_compile"]["detail"]
    assert pipeline["last_compile"]["finished_at"]
    assert pipeline["unrelated"] == "preserved"
    assert "2026-09-01.md" in pipeline["ingested"]


def test_run_compile_records_failure_without_advancing_checkpoint(store, monkeypatch):
    service = install_model(monkeypatch, "invalid JSON")

    assert asyncio.run(service.run_compile(store)) == 1

    pipeline = store.get_state("pipeline")
    assert pipeline["last_compile"]["status"] == "failed"
    assert SOURCE in pipeline["last_compile"]["detail"]
    assert "JSON" in pipeline["last_compile"]["detail"]
    assert not pipeline.get("ingested")


def test_run_compile_records_up_to_date_after_previous_failure(store, monkeypatch):
    service = install_model(monkeypatch, json.dumps({"articles": [article()]}))
    assert asyncio.run(service.run_compile(store)) == 0
    store.update_state("pipeline", lambda state: state.update(last_compile={"status": "failed"}))

    assert asyncio.run(service.run_compile(store)) == 0

    status = store.get_state("pipeline")["last_compile"]
    assert status["status"] == "up_to_date"
    assert "up to date" in status["detail"]


def test_run_compile_records_missing_source_failure(store, monkeypatch):
    service = install_model(monkeypatch, "must not be called")

    assert asyncio.run(service.run_compile(store, source="daily/2026-09-03.md")) == 1

    status = store.get_state("pipeline")["last_compile"]
    assert status["status"] == "failed"
    assert "2026-09-03.md" in status["detail"]


def test_run_compile_records_invalid_source_name_failure(store, monkeypatch):
    service = install_model(monkeypatch, "must not be called")

    assert asyncio.run(service.run_compile(store, source="notes.md")) == 1

    status = store.get_state("pipeline")["last_compile"]
    assert status["status"] == "failed"
    assert "notes.md" in status["detail"]


def test_explicit_up_to_date_source_records_no_work(store, monkeypatch):
    service = install_model(monkeypatch, json.dumps({"articles": [article()]}))
    assert asyncio.run(service.run_compile(store)) == 0

    assert asyncio.run(service.run_compile(store, source=SOURCE)) == 0

    assert store.get_state("pipeline")["last_compile"]["status"] == "up_to_date"


def test_run_compile_records_export_failure_after_successful_commit(store, monkeypatch):
    service = install_model(monkeypatch, json.dumps({"articles": [article()]}))
    monkeypatch.setattr(service, "export_committed", lambda store: "export conflict")

    assert asyncio.run(service.run_compile(store)) == 1

    pipeline = store.get_state("pipeline")
    assert pipeline["last_compile"]["status"] == "failed"
    assert "export conflict" in pipeline["last_compile"]["detail"]
    assert "2026-09-01.md" in pipeline["ingested"]


def test_compile_dry_run_does_not_replace_previous_status(store, monkeypatch):
    service = install_model(monkeypatch, "must not be called")
    store.set_state("pipeline", {"last_compile": {"status": "failed", "detail": "previous failure"}})
    previous = store.snapshot()

    assert asyncio.run(service.run_compile(store, dry_run=True)) == 0

    assert store.snapshot() == previous
