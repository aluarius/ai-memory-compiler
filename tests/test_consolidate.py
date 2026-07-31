from __future__ import annotations

import asyncio
from pathlib import Path

import consolidate


HEADER = "# Index\n\n| Article | Summary | Compiled From | Updated |\n|---|---|---|---|\n"


def _setup_kb(monkeypatch, tmp_path: Path) -> Path:
    knowledge_dir = tmp_path / "knowledge"
    concepts = knowledge_dir / "concepts"
    concepts.mkdir(parents=True)
    monkeypatch.setattr(consolidate, "KNOWLEDGE_DIR", knowledge_dir)
    monkeypatch.setattr(consolidate, "INDEX_FILE", knowledge_dir / "index.md")
    monkeypatch.setattr(consolidate, "MANIFEST_FILE", tmp_path / "manifest.txt")
    monkeypatch.setattr(
        consolidate, "list_wiki_articles", lambda: sorted(concepts.glob("*.md"))
    )
    return knowledge_dir


def test_select_candidates_carries_index_dates(monkeypatch, tmp_path: Path) -> None:
    kb = _setup_kb(monkeypatch, tmp_path)
    (kb / "concepts" / "alpha.md").write_text("word " * 300, encoding="utf-8")
    (kb / "concepts" / "beta.md").write_text("word " * 300, encoding="utf-8")
    (kb / "index.md").write_text(HEADER + "\n".join([
        "| [[concepts/alpha]] | a | daily/a.md | 2026-01-01 |",
        "| [[concepts/beta]] | b | daily/b.md | 2026-02-02 |",
    ]) + "\n", encoding="utf-8")
    monkeypatch.setattr(
        consolidate.kb_db, "find_similar_pairs",
        lambda limit=20: [
            {"a": "concepts/alpha", "b": "concepts/beta", "score": 1.0, "linked": False},
        ],
    )

    cands = consolidate.select_candidates()

    assert cands[0]["updated_a"] == "2026-01-01"
    assert cands[0]["updated_b"] == "2026-02-02"


def test_apply_manifest_deletes_file_and_index_row(monkeypatch, tmp_path: Path) -> None:
    kb = _setup_kb(monkeypatch, tmp_path)
    (kb / "concepts" / "gone.md").write_text("thin", encoding="utf-8")
    (kb / "concepts" / "hub.md").write_text("no links here", encoding="utf-8")
    (kb / "index.md").write_text(HEADER + "\n".join([
        "| [[concepts/gone]] | thin | daily/a.md | 2026-01-01 |",
        "| [[concepts/hub]] | hub | daily/b.md | 2026-01-01 |",
    ]) + "\n", encoding="utf-8")
    consolidate.MANIFEST_FILE.write_text(
        "DELETE concepts/gone\nDELETE ../etc/passwd\nnoise\n", encoding="utf-8"
    )

    deleted = consolidate.apply_manifest()

    assert deleted == ["concepts/gone"]
    assert not (kb / "concepts" / "gone.md").exists()
    index_text = (kb / "index.md").read_text(encoding="utf-8")
    assert "concepts/gone" not in index_text
    assert "[[concepts/hub]]" in index_text
    assert not consolidate.MANIFEST_FILE.exists()


def test_apply_manifest_skips_articles_with_inbound_links(monkeypatch, tmp_path: Path) -> None:
    kb = _setup_kb(monkeypatch, tmp_path)
    (kb / "concepts" / "linked.md").write_text("thin", encoding="utf-8")
    (kb / "concepts" / "hub.md").write_text("see [[concepts/linked]]", encoding="utf-8")
    (kb / "index.md").write_text(
        HEADER + "| [[concepts/linked]] | thin | daily/a.md | 2026-01-01 |\n",
        encoding="utf-8",
    )
    consolidate.MANIFEST_FILE.write_text("DELETE concepts/linked", encoding="utf-8")

    assert consolidate.apply_manifest() == []
    assert (kb / "concepts" / "linked.md").exists()


def test_run_consolidation_rolls_back_on_llm_failure(monkeypatch, tmp_path: Path) -> None:
    kb = _setup_kb(monkeypatch, tmp_path)
    for slug in ("alpha", "beta"):
        (kb / "concepts" / f"{slug}.md").write_text("word " * 300, encoding="utf-8")
    (kb / "index.md").write_text(HEADER + "\n".join([
        "| [[concepts/alpha]] | a | daily/a.md | 2026-01-01 |",
        "| [[concepts/beta]] | b | daily/b.md | 2026-01-02 |",
    ]) + "\n", encoding="utf-8")
    monkeypatch.setattr(
        consolidate.kb_db, "find_similar_pairs",
        lambda limit=20: [
            {"a": "concepts/alpha", "b": "concepts/beta", "score": 1.0, "linked": False},
        ],
    )
    calls: list[str] = []
    monkeypatch.setattr(consolidate, "ensure_kb_repo", lambda: calls.append("ensure"))
    monkeypatch.setattr(consolidate, "kb_commit", lambda msg: calls.append(f"commit:{msg}"))
    monkeypatch.setattr(consolidate, "kb_rollback", lambda: calls.append("rollback"))

    async def broken(prompt: str) -> None:
        raise RuntimeError("SDK down")

    monkeypatch.setattr(consolidate, "_run_llm_agent", broken)

    assert asyncio.run(consolidate.run_consolidation()) is False
    assert "rollback" in calls
    assert any(c.startswith("commit:checkpoint") for c in calls)


# ---------------------------------------------------------------------------
# Similarity-driven candidate selection
# ---------------------------------------------------------------------------


def test_select_candidates_uses_similar_pairs(monkeypatch, tmp_path: Path) -> None:
    kb = _setup_kb(monkeypatch, tmp_path)
    for slug in ("alpha", "beta", "gamma"):
        (kb / "concepts" / f"{slug}.md").write_text("word " * 300, encoding="utf-8")
    monkeypatch.setattr(
        consolidate.kb_db, "find_similar_pairs",
        lambda limit=20: [
            {"a": "concepts/alpha", "b": "concepts/beta", "score": 2.0, "linked": False},
            {"a": "concepts/beta", "b": "concepts/gamma", "score": 0.5, "linked": True},
        ],
    )

    cands = consolidate.select_candidates()

    assert [(c["a"], c["b"]) for c in cands] == [
        ("concepts/alpha", "concepts/beta"),
        ("concepts/beta", "concepts/gamma"),
    ]
    assert cands[0]["linked"] is False


def test_select_candidates_drops_pairs_with_missing_files(monkeypatch, tmp_path: Path) -> None:
    kb = _setup_kb(monkeypatch, tmp_path)
    (kb / "concepts" / "alpha.md").write_text("word " * 300, encoding="utf-8")
    monkeypatch.setattr(
        consolidate.kb_db, "find_similar_pairs",
        lambda limit=20: [
            {"a": "concepts/alpha", "b": "concepts/vanished", "score": 2.0, "linked": False},
        ],
    )

    assert consolidate.select_candidates() == []


def test_select_candidates_empty_without_index(monkeypatch, tmp_path: Path) -> None:
    _setup_kb(monkeypatch, tmp_path)
    monkeypatch.setattr(consolidate.kb_db, "find_similar_pairs", lambda limit=20: None)

    assert consolidate.select_candidates() == []


def test_build_prompt_offers_fold_link_keep(monkeypatch, tmp_path: Path) -> None:
    kb = _setup_kb(monkeypatch, tmp_path)
    for slug in ("alpha", "beta"):
        (kb / "concepts" / f"{slug}.md").write_text(
            f"---\ntitle: {slug}\n---\n\nBody of {slug}.\n", encoding="utf-8"
        )
    (kb / "index.md").write_text(HEADER, encoding="utf-8")
    cands = [{
        "a": "concepts/alpha", "b": "concepts/beta", "score": 2.0, "linked": False,
        "updated_a": "2026-05-01", "updated_b": "2026-05-02",
    }]

    prompt = consolidate.build_consolidation_prompt(cands)

    assert "concepts/alpha" in prompt and "concepts/beta" in prompt
    for verdict in ("FOLD", "LINK", "KEEP"):
        assert verdict in prompt
    assert "Body of alpha." in prompt  # full content of both sides
    assert "DELETE concepts/x" in prompt  # manifest protocol preserved
