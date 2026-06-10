from __future__ import annotations

import importlib.util
from datetime import datetime, timezone
from pathlib import Path


def load_session_start_module():
    root = Path(__file__).resolve().parent.parent
    module_path = root / "hooks" / "session-start.py"
    spec = importlib.util.spec_from_file_location("session_start_hook", module_path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


SAMPLE_INDEX = """# Knowledge Base Index

| Article | Summary | Compiled From | Updated |
|---------|---------|---------------|---------|
| [[concepts/fresh-topic]] | Updated yesterday | daily/2026-06-09.md | 2026-06-09 |
| [[concepts/old-single]] | One old source | daily/archive/2026-04-10.md | 2026-04-10 |
| [[concepts/old-hub]] | Big accumulating hub | daily/a.md, daily/b.md, daily/c.md, daily/d.md | 2026-05-01 |
| [[concepts/mid-topic]] | Updated within window | daily/2026-06-01.md | 2026-06-01 |
| [[concepts/old-pair]] | Two sources | daily/a.md, daily/b.md | 2026-04-20 |
"""

NOW = datetime(2026, 6, 10, 12, 0, tzinfo=timezone.utc)


def test_parse_index_rows_extracts_all_data_rows():
    mod = load_session_start_module()
    rows = mod.parse_index_rows(SAMPLE_INDEX)
    assert len(rows) == 5
    assert rows[0]["link"] == "[[concepts/fresh-topic]]"
    assert rows[0]["updated"] == "2026-06-09"
    assert rows[2]["source_count"] == 4


def test_parse_index_rows_skips_header_and_prose():
    mod = load_session_start_module()
    rows = mod.parse_index_rows("# Title\n\nSome prose\n\n| A | B |\n")
    assert rows == []


def test_select_tier_recent_newest_first():
    mod = load_session_start_module()
    rows = mod.parse_index_rows(SAMPLE_INDEX)
    recent, hubs = mod.select_tier_rows(rows, NOW, recent_days=14, max_hubs=10)
    assert [r["link"] for r in recent] == [
        "[[concepts/fresh-topic]]",
        "[[concepts/mid-topic]]",
    ]


def test_select_tier_hubs_by_source_count_excluding_recent():
    mod = load_session_start_module()
    rows = mod.parse_index_rows(SAMPLE_INDEX)
    recent, hubs = mod.select_tier_rows(rows, NOW, recent_days=14, max_hubs=10)
    hub_links = [r["link"] for r in hubs]
    assert hub_links[0] == "[[concepts/old-hub]]"  # 4 sources
    assert "[[concepts/old-pair]]" in hub_links  # 2 sources
    assert "[[concepts/old-single]]" not in hub_links  # 1 source — not a hub
    assert "[[concepts/fresh-topic]]" not in hub_links  # already in recent


def test_build_kb_section_fits_budget_and_keeps_pointer():
    mod = load_session_start_module()
    rows = mod.parse_index_rows(SAMPLE_INDEX)
    section = mod.build_kb_section(rows, NOW, budget=20_000)
    assert len(section) < 20_000
    assert "search_knowledge" in section
    assert "[[concepts/fresh-topic]]" in section
    assert "[[concepts/old-hub]]" in section


def test_build_kb_section_drops_rows_not_mid_truncates():
    mod = load_session_start_module()
    # Many rows with long summaries against a tiny budget
    big_index = "\n".join(
        f"| [[concepts/topic-{i:03d}]] | {'x' * 200} | daily/2026-06-09.md | 2026-06-09 |"
        for i in range(50)
    )
    rows = mod.parse_index_rows(big_index)
    assert len(rows) == 50
    section = mod.build_kb_section(rows, NOW, budget=2000)
    assert len(section) <= 2000
    # Every emitted row must be complete (ends with |)
    for line in section.splitlines():
        if line.startswith("| [[concepts/topic-"):
            assert line.endswith(" |")


def test_build_context_under_cap_with_real_kb():
    mod = load_session_start_module()
    context = mod.build_context()
    assert len(context) <= mod.MAX_CONTEXT_CHARS
    assert "...(truncated)" not in context
    # Recent log section always present and BEFORE the KB section
    log_pos = context.find("## Recent Daily Log")
    kb_pos = context.find("## Knowledge Base")
    assert log_pos != -1
    if kb_pos != -1:
        assert log_pos < kb_pos
