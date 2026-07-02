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


def test_select_candidates_sparse_and_old_only(monkeypatch, tmp_path: Path) -> None:
    kb = _setup_kb(monkeypatch, tmp_path)
    (kb / "concepts" / "thin-old.md").write_text("few words", encoding="utf-8")
    (kb / "concepts" / "thin-fresh.md").write_text("few words", encoding="utf-8")
    (kb / "concepts" / "big-old.md").write_text("word " * 300, encoding="utf-8")
    (kb / "index.md").write_text(HEADER + "\n".join([
        "| [[concepts/thin-old]] | a | daily/a.md | 2026-01-01 |",
        "| [[concepts/thin-fresh]] | b | daily/b.md | 2099-01-01 |",
        "| [[concepts/big-old]] | c | daily/c.md | 2026-01-01 |",
    ]) + "\n", encoding="utf-8")

    targets = [c["target"] for c in consolidate.select_candidates()]

    assert targets == ["concepts/thin-old"]


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
    (kb / "concepts" / "thin-old.md").write_text("few words", encoding="utf-8")
    (kb / "index.md").write_text(
        HEADER + "| [[concepts/thin-old]] | a | daily/a.md | 2026-01-01 |\n",
        encoding="utf-8",
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
