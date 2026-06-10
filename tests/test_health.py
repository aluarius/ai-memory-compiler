from __future__ import annotations

import json
from pathlib import Path

import health


def _write_state(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def test_collect_health_reports_attention_items(monkeypatch, tmp_path: Path) -> None:
    daily_dir = tmp_path / "daily"
    archive_dir = daily_dir / "archive"
    knowledge_dir = tmp_path / "knowledge"
    reports_dir = tmp_path / "reports"
    scripts_dir = tmp_path / "scripts"
    state_file = scripts_dir / "state.json"

    (knowledge_dir / "concepts").mkdir(parents=True)
    archive_dir.mkdir(parents=True)
    reports_dir.mkdir()
    scripts_dir.mkdir()

    (knowledge_dir / "concepts" / "example.md").write_text("# Example", encoding="utf-8")
    (daily_dir / "2026-06-04.md").write_text("# Daily Log", encoding="utf-8")
    (archive_dir / "2026-05-01.md").write_text("# Archived", encoding="utf-8")
    failed_dir = reports_dir / "failed-flushes"
    failed_dir.mkdir()
    (failed_dir / "session-flush-example.md").write_text("context", encoding="utf-8")
    (scripts_dir / "import-flush-example.md").write_text("context", encoding="utf-8")
    (scripts_dir / "compile.log").write_text(
        "Compilation failed. Total cost: $0.00\nFailed logs: 2026-06-04.md\n",
        encoding="utf-8",
    )
    (scripts_dir / "flush.log").write_text("2026-06-04 INFO saved to daily log\n", encoding="utf-8")
    _write_state(
        state_file,
        {
            "ingested": {},
            "last_lint": "2026-06-04T10:00:00+00:00",
            "total_cost": 1.25,
        },
    )

    monkeypatch.setattr(health, "DAILY_DIR", daily_dir)
    monkeypatch.setattr(health, "DAILY_ARCHIVE_DIR", archive_dir)
    monkeypatch.setattr(health, "KNOWLEDGE_DIR", knowledge_dir)
    monkeypatch.setattr(health, "REPORTS_DIR", reports_dir)
    monkeypatch.setattr(health, "SCRIPTS_DIR", scripts_dir)
    monkeypatch.setattr(health, "STATE_FILE", state_file)
    monkeypatch.setattr(health, "FAILED_FLUSH_DIR", failed_dir)
    monkeypatch.setattr(health, "COMPILE_LOG_FILE", scripts_dir / "compile.log")
    monkeypatch.setattr(health, "FLUSH_LOG_FILE", scripts_dir / "flush.log")
    monkeypatch.setattr(health, "run_structural_checks", lambda: [])
    monkeypatch.setattr(
        health,
        "load_runtime_config",
        lambda: {
            "flush_runtime": "claude",
            "compile_runtime": "claude",
            "query_runtime": "claude",
            "lint_runtime": "claude",
        },
    )

    report = health.collect_health()

    assert report.status == "attention"
    assert report.article_count == 1
    assert report.daily_log_count == 1
    assert report.archived_daily_log_count == 1
    assert report.uncompiled_daily_logs == ["2026-06-04.md"]
    assert report.failed_flush_contexts == ["failed-flushes/session-flush-example.md"]
    assert report.pending_flush_contexts == ["import-flush-example.md"]
    assert report.last_compile.status == "failed"
    assert report.last_compile.detail == (
        "Compilation failed. Total cost: $0.00; Failed logs: 2026-06-04.md"
    )


def test_format_report_includes_actionable_sections() -> None:
    report = health.HealthReport(
        status="attention",
        issue_counts=health.IssueCounts(total=1, errors=0, warnings=1, suggestions=0),
        article_count=10,
        daily_log_count=2,
        archived_daily_log_count=1,
        uncompiled_daily_logs=["2026-06-04.md"],
        stale_daily_logs=[],
        permanent_failed_contexts=[],
        failed_flush_contexts=["failed-flushes/context.md"],
        pending_flush_contexts=[],
        last_compile=health.PipelineLogStatus(status="failed", detail="Compilation failed"),
        last_flush_line="INFO saved to daily log",
        last_lint="2026-06-04T10:00:00+00:00",
        total_cost=2.5,
        runtime_config={
            "flush_runtime": "claude",
            "compile_runtime": "claude",
            "query_runtime": "claude",
            "lint_runtime": "claude",
        },
    )

    text = health.format_report(report)

    assert "Status: attention" in text
    assert "- Structural lint: 0 errors, 1 warnings, 0 suggestions" in text
    assert "Failed flush contexts" in text
    assert "uv run python scripts/compile.py --dry-run" in text


def test_exit_code_only_fails_attention_items_in_strict_mode() -> None:
    report = health.HealthReport(
        status="attention",
        issue_counts=health.IssueCounts(total=0, errors=0, warnings=0, suggestions=0),
        article_count=0,
        daily_log_count=0,
        archived_daily_log_count=0,
        uncompiled_daily_logs=[],
        stale_daily_logs=[],
        permanent_failed_contexts=[],
        failed_flush_contexts=["failed-flushes/context.md"],
        pending_flush_contexts=[],
        last_compile=health.PipelineLogStatus(status="complete", detail=None),
        last_flush_line=None,
        last_lint=None,
        total_cost=0.0,
        runtime_config={},
    )

    assert health.exit_code(report, strict=False) == 0
    assert health.exit_code(report, strict=True) == 1


def test_exit_code_fails_structural_errors_without_strict_mode() -> None:
    report = health.HealthReport(
        status="unhealthy",
        issue_counts=health.IssueCounts(total=1, errors=1, warnings=0, suggestions=0),
        article_count=0,
        daily_log_count=0,
        archived_daily_log_count=0,
        uncompiled_daily_logs=[],
        stale_daily_logs=[],
        permanent_failed_contexts=[],
        failed_flush_contexts=[],
        pending_flush_contexts=[],
        last_compile=health.PipelineLogStatus(status="complete", detail=None),
        last_flush_line=None,
        last_lint=None,
        total_cost=0.0,
        runtime_config={},
    )

    assert health.exit_code(report, strict=False) == 2
