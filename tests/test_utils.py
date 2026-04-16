from __future__ import annotations

from pathlib import Path

import compile as compile_script
import utils


def test_safe_join_blocks_path_escape(tmp_path: Path) -> None:
    root = tmp_path / "knowledge"
    root.mkdir()

    allowed = utils.safe_join(root, "concepts/example.md")
    blocked = utils.safe_join(root, "../secrets.txt")

    assert allowed == (root / "concepts" / "example.md").resolve()
    assert blocked is None


def test_daily_source_exists_checks_archive(monkeypatch, tmp_path: Path) -> None:
    daily_dir = tmp_path / "daily"
    archive_dir = daily_dir / "archive"
    archive_dir.mkdir(parents=True)
    archived = archive_dir / "2026-04-10.md"
    archived.write_text("# archived", encoding="utf-8")

    monkeypatch.setattr(utils, "DAILY_DIR", daily_dir)
    monkeypatch.setattr(utils, "DAILY_ARCHIVE_DIR", archive_dir)

    assert utils.daily_source_exists("daily/2026-04-10")
    assert utils.daily_source_exists("daily/archive/2026-04-10")


def test_rewrite_archived_source_refs_updates_wikilinks_and_frontmatter(
    monkeypatch,
    tmp_path: Path,
) -> None:
    knowledge_dir = tmp_path / "knowledge"
    knowledge_dir.mkdir()
    article = knowledge_dir / "example.md"
    article.write_text(
        'sources:\n  - "daily/2026-04-10.md"\n\n[[daily/2026-04-10]]\nCompiled From: daily/2026-04-10.md\n- Source: daily/2026-04-10.md\n',
        encoding="utf-8",
    )

    monkeypatch.setattr(compile_script, "KNOWLEDGE_DIR", knowledge_dir)

    compile_script.rewrite_archived_source_refs("2026-04-10.md")

    updated = article.read_text(encoding="utf-8")
    assert '"daily/archive/2026-04-10.md"' in updated
    assert "[[daily/archive/2026-04-10]]" in updated
    assert "Compiled From: daily/archive/2026-04-10.md" in updated
    assert "- Source: daily/archive/2026-04-10.md" in updated
