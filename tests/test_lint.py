from __future__ import annotations

from pathlib import Path

import lint


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
