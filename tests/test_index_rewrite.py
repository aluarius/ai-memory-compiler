from __future__ import annotations

import asyncio
from pathlib import Path

import index_rewrite


HEADER = "# Index\n\n| Article | Summary | Compiled From | Updated |\n|---|---|---|---|\n"


def _setup_index(monkeypatch, tmp_path: Path, rows: list[str]) -> Path:
    knowledge_dir = tmp_path / "knowledge"
    (knowledge_dir / "concepts").mkdir(parents=True)
    index = knowledge_dir / "index.md"
    index.write_text(HEADER + "\n".join(rows) + "\n", encoding="utf-8")
    monkeypatch.setattr(index_rewrite, "INDEX_FILE", index)
    monkeypatch.setattr(index_rewrite, "KNOWLEDGE_DIR", knowledge_dir)
    return index


def test_collect_targets_finds_long_and_stub_rows(monkeypatch, tmp_path: Path) -> None:
    long_summary = "x" * 250
    rows = [
        f"| [[concepts/bloated]] | {long_summary} | daily/a.md | 2026-06-01 |",
        "| [[concepts/stubbed]] | Stubbed (auto-stub: refine summary on next compile) | daily/a.md | 2026-06-01 |",
        "| [[concepts/clean]] | fine | daily/a.md | 2026-06-01 |",
    ]
    index = _setup_index(monkeypatch, tmp_path, rows)
    article = index.parent / "concepts" / "stubbed.md"
    article.write_text("---\ntitle: Stubbed\n---\n\nReal body content here.\n", encoding="utf-8")

    targets = index_rewrite.collect_rewrite_targets()

    by_target = {t["target"]: t for t in targets}
    assert by_target["concepts/bloated"]["kind"] == "long"
    assert by_target["concepts/stubbed"]["kind"] == "stub"
    assert "Real body content" in by_target["concepts/stubbed"]["excerpt"]
    assert "concepts/clean" not in by_target


def test_parse_response_validates_lines(monkeypatch, tmp_path: Path) -> None:
    targets = [
        {"target": "concepts/a", "summary": "x" * 250, "kind": "long", "excerpt": None},
        {"target": "concepts/b", "summary": "y" * 250, "kind": "long", "excerpt": None},
        {"target": "concepts/c", "summary": "z" * 250, "kind": "long", "excerpt": None},
    ]
    response = "\n".join([
        "concepts/a: Short essence line.",
        "[[concepts/b]]: Bracketed target accepted.",
        "concepts/c: bad " + "c" * 300,          # too long -> rejected
        "concepts/unknown: not requested",        # unknown -> rejected
    ])

    parsed = index_rewrite.parse_rewrite_response(response, targets)

    assert parsed == {
        "concepts/a": "Short essence line.",
        "concepts/b": "Bracketed target accepted.",
    }


def test_parse_response_rejects_pipes_and_wikilinks(monkeypatch, tmp_path: Path) -> None:
    targets = [
        {"target": "concepts/a", "summary": "x" * 250, "kind": "long", "excerpt": None},
        {"target": "concepts/b", "summary": "y" * 250, "kind": "long", "excerpt": None},
    ]
    response = "\n".join([
        "concepts/a: pipe | breaks tables",
        "concepts/b: link [[concepts/a]] not allowed",
    ])

    assert index_rewrite.parse_rewrite_response(response, targets) == {}


def test_apply_rewrites_replaces_only_summary_cell(monkeypatch, tmp_path: Path) -> None:
    rows = [
        "| [[concepts/a]] | " + "x" * 250 + " | daily/a.md, daily/b.md | 2026-06-01 |",
        "| [[concepts/keep]] | untouched | daily/k.md | 2026-05-05 |",
    ]
    index = _setup_index(monkeypatch, tmp_path, rows)

    changed = index_rewrite.apply_rewrites({"concepts/a": "New essence."})

    assert changed == 1
    text = index.read_text(encoding="utf-8")
    assert "| [[concepts/a]] | New essence. | daily/a.md, daily/b.md | 2026-06-01 |" in text
    assert "| [[concepts/keep]] | untouched | daily/k.md | 2026-05-05 |" in text


def test_run_summary_rewrite_end_to_end_with_fake_llm(monkeypatch, tmp_path: Path) -> None:
    rows = ["| [[concepts/a]] | " + "x" * 250 + " | daily/a.md | 2026-06-01 |"]
    index = _setup_index(monkeypatch, tmp_path, rows)

    async def fake_llm(prompt: str) -> str:
        assert "concepts/a" in prompt
        return "concepts/a: Rewritten."

    monkeypatch.setattr(index_rewrite, "_call_llm", fake_llm)

    changed = asyncio.run(index_rewrite.run_summary_rewrite())

    assert changed == 1
    assert "Rewritten." in index.read_text(encoding="utf-8")


def test_run_summary_rewrite_survives_llm_failure(monkeypatch, tmp_path: Path) -> None:
    rows = ["| [[concepts/a]] | " + "x" * 250 + " | daily/a.md | 2026-06-01 |"]
    _setup_index(monkeypatch, tmp_path, rows)

    async def broken_llm(prompt: str) -> str:
        raise RuntimeError("SDK down")

    monkeypatch.setattr(index_rewrite, "_call_llm", broken_llm)

    assert asyncio.run(index_rewrite.run_summary_rewrite()) == 0


def test_parse_response_truncates_slightly_over_limit_at_clause(monkeypatch, tmp_path: Path) -> None:
    targets = [
        {"target": "concepts/a", "summary": "x" * 250, "kind": "long", "excerpt": None},
        {"target": "concepts/b", "summary": "y" * 250, "kind": "long", "excerpt": None},
    ]
    # 3 clauses; full line > 200, first two clauses fit
    clause = "k" * 90
    over = f"{clause}; {clause}; {clause}"
    no_boundary = "z" * 220  # over limit, no clause boundary -> rejected
    response = f"concepts/a: {over}\nconcepts/b: {no_boundary}"

    parsed = index_rewrite.parse_rewrite_response(response, targets)

    assert parsed["concepts/a"] == f"{clause}; {clause}"
    assert len(parsed["concepts/a"]) <= index_rewrite.MAX_SUMMARY_CHARS
    assert "concepts/b" not in parsed
