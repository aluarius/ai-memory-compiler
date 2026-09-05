from __future__ import annotations

import asyncio
from contextlib import nullcontext
from pathlib import Path

import lint
import pytest
from memory_export import ExportConflict, export_memory
from memory_store import MemoryStore
from model_runtime import ModelResult


def _canonical_graph(monkeypatch, tmp_path: Path) -> MemoryStore:
    store = MemoryStore(tmp_path)
    store.initialize()
    store.import_source("daily/2026-09-01.md", "Source")
    changes = []
    for name, links in [("alpha", "[[concepts/beta]]"), ("beta", "")]:
        changes.append({
            "path": f"concepts/{name}", "summary": name,
            "body": f"---\ntitle: {name}\nsources: [daily/2026-09-01.md]\ncreated: 2026-09-01\nupdated: 2026-09-01\n---\n# {name}\n{links}\n[[daily/2026-09-01]]\n",
        })
    store.commit_articles(changes)
    monkeypatch.setattr(lint, "KNOWLEDGE_DIR", tmp_path / "knowledge")
    return store


def test_canonical_lint_reads_each_article_once_without_exports(monkeypatch, tmp_path: Path) -> None:
    _canonical_graph(monkeypatch, tmp_path)
    calls = []
    extract = lint.extract_wikilinks
    def observe(text: str) -> list[str]:
        calls.append(text)
        return extract(text)
    monkeypatch.setattr(lint, "extract_wikilinks", observe)
    issues = lint.structural_checks()
    assert len(calls) == 2
    assert not [item for item in issues if item["severity"] == "error"]
    backlinks = [item for item in issues if item["check"] == "missing_backlink"]
    assert [(item["source"], item["target"]) for item in backlinks] == [("concepts/alpha", "concepts/beta")]


def test_canonical_mechanical_fixes_commit_then_export(monkeypatch, tmp_path: Path) -> None:
    store = _canonical_graph(monkeypatch, tmp_path)
    counts = lint.apply_fixes(lint.structural_checks())
    assert counts["backlinks_added"] == 1
    beta = store.read_article("concepts/beta")
    assert beta["revision"] == 2
    assert "[[concepts/alpha]]" in beta["body"]
    assert (tmp_path / "knowledge/concepts/beta.md").read_text() == beta["body"]
    assert not [item for item in lint.structural_checks() if item["check"] == "missing_backlink"]


def test_canonical_fix_preserves_external_export_edits(monkeypatch, tmp_path: Path) -> None:
    store = _canonical_graph(monkeypatch, tmp_path)
    export_memory(store)
    path = tmp_path / "knowledge/concepts/beta.md"
    path.write_text("External edits must survive")
    with pytest.raises(ExportConflict):
        lint.apply_fixes(lint.structural_checks())
    assert path.read_text() == "External edits must survive"
    assert store.read_article("concepts/beta")["revision"] == 2
    assert "[[concepts/alpha]]" in store.read_article("concepts/beta")["body"]


def test_lint_corrupt_canonical_database_does_not_read_legacy_files(monkeypatch, tmp_path: Path) -> None:
    _canonical_graph(monkeypatch, tmp_path)
    (tmp_path / "scripts/memory.sqlite").write_bytes(b"invalid sqlite")
    def legacy_read() -> list[Path]:
        raise AssertionError("Canonical corruption must not select Markdown")
    monkeypatch.setattr(lint, "list_wiki_articles", legacy_read)
    issues = lint.structural_checks()
    assert len(issues) == 1
    assert issues[0]["check"] == "database"
    assert issues[0]["severity"] == "error"


def test_canonical_semantic_candidates_use_database_bodies(monkeypatch, tmp_path: Path) -> None:
    _canonical_graph(monkeypatch, tmp_path)
    monkeypatch.setattr(lint.kb_db, "find_similar_pairs", lambda limit: [{"a": "concepts/alpha", "b": "concepts/beta"}])
    blocks = lint._semantic_candidate_blocks()
    assert len(blocks) == 1
    assert "# alpha" in blocks[0]
    assert "# beta" in blocks[0]


def test_canonical_semantic_lint_uses_disposable_readonly_runtime_and_rejects_invalid_result(monkeypatch, tmp_path: Path) -> None:
    store = _canonical_graph(monkeypatch, tmp_path)
    monkeypatch.setattr(lint.kb_db, "find_similar_pairs", lambda limit: [{"a": "concepts/alpha", "b": "concepts/beta"}])
    monkeypatch.setattr(lint, "file_lock", lambda _: nullcontext())
    seen = []
    async def call(prompt: str, *, cwd: Path, task: str) -> ModelResult:
        assert not cwd.is_relative_to(tmp_path)
        assert task == "lint"
        assert "# alpha" in prompt
        seen.append(cwd)
        return ModelResult(text="unstructured output")
    monkeypatch.setattr(lint, "call_readonly_model", call)
    before = store.snapshot()
    issues = asyncio.run(lint.check_contradictions())
    assert issues[0]["severity"] == "error"
    assert store.snapshot() == before
    assert seen and not seen[0].exists()


def test_legacy_orphan_check_reads_linear_number_of_bodies(monkeypatch, tmp_path: Path) -> None:
    directory = tmp_path / "knowledge/concepts"
    directory.mkdir(parents=True)
    articles = [directory / f"article-{i}.md" for i in range(8)]
    for article in articles:
        article.write_text("[[concepts/article-0]]")
    monkeypatch.setattr(lint, "KNOWLEDGE_DIR", directory.parent)
    monkeypatch.setattr(lint, "list_wiki_articles", lambda: articles)
    calls = []
    original = Path.read_text
    def observe(path: Path, *args, **kwargs) -> str:
        if path in articles:
            calls.append(path)
        return original(path, *args, **kwargs)
    monkeypatch.setattr(Path, "read_text", observe)
    lint.check_orphan_pages()
    assert len(calls) == len(articles)


def test_contradiction_check_uses_bounded_full_text_candidates(
    monkeypatch, tmp_path: Path
) -> None:
    """Semantic lint receives only bounded, full-text candidate pairs."""
    knowledge_dir = tmp_path / "knowledge"
    concepts_dir = knowledge_dir / "concepts"
    concepts_dir.mkdir(parents=True)
    (concepts_dir / "alpha.md").write_text("Alpha source body", encoding="utf-8")
    (concepts_dir / "beta.md").write_text("Beta source body", encoding="utf-8")
    observed: dict[str, object] = {}

    monkeypatch.setattr(lint, "KNOWLEDGE_DIR", knowledge_dir)
    monkeypatch.setattr(
        lint.kb_db,
        "find_similar_pairs",
        lambda limit: [{"a": "concepts/alpha", "b": "concepts/beta", "score": 1.0}],
    )
    monkeypatch.setattr(lint, "get_task_runtime", lambda _: "codex")
    monkeypatch.setattr(lint, "get_codex_model", lambda: None)
    monkeypatch.setattr(lint, "file_lock", lambda _: nullcontext())

    def fake_run(prompt: str, **kwargs: object) -> str:
        observed["prompt"] = prompt
        observed["kwargs"] = kwargs
        return "NO_ISSUES"

    monkeypatch.setattr(lint, "run_codex_prompt", fake_run)

    assert asyncio.run(lint.check_contradictions()) == []
    prompt = str(observed["prompt"])
    assert "Alpha source body" in prompt
    assert "Beta source body" in prompt
    assert "Candidate Article Pairs" in prompt
    assert observed["kwargs"] == {
        "cwd": lint.ROOT_DIR,
        "allow_edits": False,
        "model": None,
    }


def test_check_weak_connectivity_reports_low_degree_articles(monkeypatch, tmp_path: Path) -> None:
    knowledge_dir = tmp_path / "knowledge"
    concepts_dir = knowledge_dir / "concepts"
    connections_dir = knowledge_dir / "connections"
    qa_dir = knowledge_dir / "qa"
    concepts_dir.mkdir(parents=True)
    connections_dir.mkdir()
    qa_dir.mkdir()

    hub = concepts_dir / "hub.md"
    hub.write_text(
        "\n".join([
            "[[concepts/spoke-a]]",
            "[[concepts/spoke-b]]",
            "[[concepts/spoke-c]]",
        ]),
        encoding="utf-8",
    )
    spoke_a = concepts_dir / "spoke-a.md"
    spoke_a.write_text("[[concepts/hub]]\n[[concepts/spoke-b]]", encoding="utf-8")
    spoke_b = concepts_dir / "spoke-b.md"
    spoke_b.write_text("[[concepts/hub]]\n[[concepts/spoke-a]]", encoding="utf-8")
    spoke_c = concepts_dir / "spoke-c.md"
    spoke_c.write_text("[[concepts/hub]]", encoding="utf-8")

    monkeypatch.setattr(lint, "KNOWLEDGE_DIR", knowledge_dir)
    monkeypatch.setattr(lint, "list_wiki_articles", lambda: sorted(concepts_dir.glob("*.md")))

    issues = lint.check_weak_connectivity(max_issues=10)

    assert [issue["file"] for issue in issues] == ["concepts/spoke-c.md"]
    assert issues[0]["check"] == "weak_connectivity"


# ---------------------------------------------------------------------------
# Index hygiene
# ---------------------------------------------------------------------------


def _write_index(tmp_path: Path, rows: list[str]) -> Path:
    knowledge_dir = tmp_path / "knowledge"
    knowledge_dir.mkdir(parents=True, exist_ok=True)
    index = knowledge_dir / "index.md"
    header = "# Index\n\n| Article | Summary | Compiled From | Updated |\n|---|---|---|---|\n"
    index.write_text(header + "\n".join(rows) + "\n", encoding="utf-8")
    return knowledge_dir


def test_check_index_hygiene_flags_long_summary_and_source_sprawl(
    monkeypatch, tmp_path: Path
) -> None:
    long_summary = "x" * 250
    rows = [
        f"| [[concepts/bloated]] | {long_summary} | daily/a.md | 2026-06-01 |",
        "| [[concepts/sprawl]] | ok | daily/a.md, daily/b.md, daily/c.md, daily/d.md, daily/e.md | 2026-06-01 |",
        "| [[concepts/clean]] | short | daily/a.md | 2026-06-01 |",
    ]
    knowledge_dir = _write_index(tmp_path, rows)
    monkeypatch.setattr(lint, "KNOWLEDGE_DIR", knowledge_dir)

    issues = lint.check_index_hygiene()

    subchecks = {(i["subcheck"], i["target"]) for i in issues}
    assert ("long_summary", "concepts/bloated") in subchecks
    assert ("source_sprawl", "concepts/sprawl") in subchecks
    assert all(i["target"] != "concepts/clean" for i in issues)


def test_fix_index_source_sprawl_collapses_to_first_latest_count(
    monkeypatch, tmp_path: Path
) -> None:
    rows = [
        "| [[concepts/sprawl]] | ok | daily/a.md, daily/b.md, daily/c.md, daily/d.md, daily/e.md | 2026-06-01 |",
    ]
    knowledge_dir = _write_index(tmp_path, rows)
    monkeypatch.setattr(lint, "KNOWLEDGE_DIR", knowledge_dir)

    issues = lint.check_index_hygiene()
    fixed = lint.fix_index_source_sprawl(issues)

    assert fixed == 1
    content = (knowledge_dir / "index.md").read_text(encoding="utf-8")
    assert "| daily/a.md, daily/e.md +3 more |" in content
    # Re-check finds no sprawl after the fix
    remaining = [i for i in lint.check_index_hygiene() if i["subcheck"] == "source_sprawl"]
    assert remaining == []


def test_fix_index_source_sprawl_noop_on_clean_index(monkeypatch, tmp_path: Path) -> None:
    rows = ["| [[concepts/clean]] | short | daily/a.md | 2026-06-01 |"]
    knowledge_dir = _write_index(tmp_path, rows)
    monkeypatch.setattr(lint, "KNOWLEDGE_DIR", knowledge_dir)

    issues = lint.check_index_hygiene()
    assert lint.fix_index_source_sprawl(issues) == 0


def test_source_sprawl_not_autofixable_when_summary_contains_raw_pipes(
    monkeypatch, tmp_path: Path
) -> None:
    # A raw '|' inside the summary shifts cell parsing: the regex sees part of
    # the summary as the sources cell. Collapsing that cell destroys content,
    # so such rows must never be marked auto-fixable.
    rows = [
        "| [[concepts/broken]] | summary part | tail with, commas, more, stuff, here | daily/a.md | 2026-06-01 |",
        "| [[concepts/real-sprawl]] | ok | daily/a.md, daily/b.md, daily/c.md, daily/d.md, daily/e.md | 2026-06-01 |",
    ]
    knowledge_dir = _write_index(tmp_path, rows)
    monkeypatch.setattr(lint, "KNOWLEDGE_DIR", knowledge_dir)

    issues = lint.check_index_hygiene()

    sprawl = {i["target"]: i for i in issues if i.get("subcheck") == "source_sprawl"}
    assert sprawl["concepts/real-sprawl"].get("auto_fixable") is True
    assert "concepts/broken" not in sprawl or not sprawl["concepts/broken"].get("auto_fixable")
