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
