from __future__ import annotations

import json
from pathlib import Path

import health
from memory_export import export_memory
from memory_store import MemoryStore


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


def test_overall_status_attention_when_only_permanent_failures_exist() -> None:
    counts = health.IssueCounts(total=0, errors=0, warnings=0, suggestions=0)
    status = health._overall_status(
        issue_counts=counts,
        failed_flush_count=0,
        permanent_failed_count=2,
        pending_flush_count=0,
        uncompiled_count=0,
        stale_count=0,
        compile_status="complete",
    )
    assert status == "attention"


def test_overall_status_ok_when_nothing_pending() -> None:
    counts = health.IssueCounts(total=0, errors=0, warnings=0, suggestions=0)
    status = health._overall_status(
        issue_counts=counts,
        failed_flush_count=0,
        permanent_failed_count=0,
        pending_flush_count=0,
        uncompiled_count=0,
        stale_count=0,
        compile_status="complete",
    )
    assert status == "ok"


def _canonical_health(monkeypatch, tmp_path: Path) -> MemoryStore:
    store = MemoryStore(tmp_path)
    store.initialize()
    monkeypatch.setattr(health, "KNOWLEDGE_DIR", tmp_path / "knowledge")
    monkeypatch.setattr(health, "load_runtime_config", lambda: {})
    return store


def test_canonical_health_uses_database_and_accepts_today_backlog(monkeypatch, tmp_path: Path) -> None:
    store = _canonical_health(monkeypatch, tmp_path)
    store.import_source(f"daily/{health.today_iso()}.md", "Today's conversation")
    export_memory(store)
    report = health.collect_health()
    assert report.backend == "sqlite"
    assert report.daily_log_count == 1
    assert report.uncompiled_daily_logs == [f"{health.today_iso()}.md"]
    assert report.status == "ok"
    assert health.exit_code(report, strict=True) == 0
    (tmp_path / f"daily/{health.today_iso()}.md").unlink()
    report = health.collect_health()
    assert report.daily_log_count == 1
    assert report.status == "attention"
    assert any("missing" in item for item in report.export_drift)


def test_health_reports_failed_quarantined_and_expired_jobs(monkeypatch, tmp_path: Path) -> None:
    store = _canonical_health(monkeypatch, tmp_path)
    for identity, quarantine in [("failed", False), ("quarantined", True)]:
        job_id = store.enqueue("private context", {}, identity=identity)
        job = store.claim_job(job_id)
        store.fail_job(job_id, job["lease_token"], "provider failed", quarantine=quarantine)
    job_id = store.enqueue("private context", {}, identity="expired")
    store.claim_job(job_id)
    with store.transaction() as connection:
        connection.execute("UPDATE jobs SET lease_until=0 WHERE id=?", (job_id,))
    export_memory(store)
    report = health.collect_health()
    assert report.status == "attention"
    assert report.job_counts == {"failed": 1, "quarantined": 1, "running": 1, "expired": 1}
    assert len(report.operational_jobs) == 3
    assert "private context" not in health.format_report(report)
    assert health.exit_code(report, strict=True) == 1


def test_health_corrupt_database_never_falls_back_to_markdown(monkeypatch, tmp_path: Path) -> None:
    _canonical_health(monkeypatch, tmp_path)
    (tmp_path / "scripts/memory.sqlite").write_bytes(b"broken database")
    report = health.collect_health()
    assert report.backend == "sqlite"
    assert report.status == "unhealthy"
    assert report.database_errors
    assert health.exit_code(report, strict=False) == 2


def test_export_drift_checks_manifest_and_content_when_generation_is_unchanged(monkeypatch, tmp_path: Path) -> None:
    store = _canonical_health(monkeypatch, tmp_path)
    source = f"daily/{health.today_iso()}.md"
    store.import_source(source, "Original")
    export_memory(store)
    before = store.generation()
    store.append_source(source, "\nNew conversation", event_key="new")
    assert store.generation() == before
    report = health.collect_health()
    assert report.status == "attention"
    assert any(source in item for item in report.export_drift)


def test_health_current_pending_and_running_jobs_are_not_failures(monkeypatch, tmp_path: Path) -> None:
    store = _canonical_health(monkeypatch, tmp_path)
    store.enqueue("queued", {}, identity="pending")
    job_id = store.enqueue("running", {}, identity="running")
    store.claim_job(job_id)
    export_memory(store)
    report = health.collect_health()
    assert report.status == "ok"
    assert report.job_counts == {"pending": 1, "running": 1, "expired": 0}


def test_health_does_not_hide_stalled_pending_queue(monkeypatch, tmp_path):
    store = _canonical_health(monkeypatch, tmp_path)
    job_id = store.enqueue("never started", {}, identity="lost-spawn")
    with store.transaction() as connection:
        connection.execute("UPDATE jobs SET created='2020-01-01T00:00:00+00:00' WHERE id=?", (job_id,))
    export_memory(store)
    report = health.collect_health()
    assert report.status == "attention"
    assert report.job_counts["stalled_pending"] == 1
    assert report.operational_jobs[0]["id"] == job_id


def test_health_reports_unimported_cutover_spool(monkeypatch, tmp_path):
    from capture_spool import persist_spooled_context
    store = _canonical_health(monkeypatch, tmp_path)
    export_memory(store)
    path = persist_spooled_context(tmp_path, "Complete captured source", {"session_id": "s1"})
    report = health.collect_health()
    assert report.status == "attention"
    assert report.capture_spool_files == [str(path.relative_to(tmp_path))]


def test_health_accepts_migrated_short_ingestion_hash(monkeypatch, tmp_path: Path) -> None:
    store = _canonical_health(monkeypatch, tmp_path)
    store.import_source("daily/2026-01-01.md", "Already compiled")
    source = store.list_sources()[0]
    store.set_state("pipeline", {"ingested": {"2026-01-01.md": {"hash": source["content_hash"][:16]}}})
    export_memory(store)
    assert health.collect_health().status == "ok"


def test_health_reports_compile_failure_even_for_today(monkeypatch, tmp_path: Path) -> None:
    store = _canonical_health(monkeypatch, tmp_path)
    store.import_source(f"daily/{health.today_iso()}.md", "Today's conversation")
    store.set_state("pipeline", {"last_compile": {"status": "failed", "detail": "Provider timeout"}})
    export_memory(store)
    report = health.collect_health()
    assert report.status == "attention"
    assert report.last_compile.detail == "Provider timeout"


def test_export_generation_and_external_edits_are_visible(monkeypatch, tmp_path: Path) -> None:
    store = _canonical_health(monkeypatch, tmp_path)
    export_memory(store)
    (tmp_path / "knowledge/index.md").write_text("External changes")
    store.set_state("exports", {**store.get_state("exports"), "generation": -1})
    report = health.collect_health()
    assert any(item.startswith("Generation:") for item in report.export_drift)
    assert "Export modified: knowledge/index.md" in report.export_drift
    assert report.status == "attention"


def test_health_includes_uncompiled_archived_sources(monkeypatch, tmp_path: Path) -> None:
    store = _canonical_health(monkeypatch, tmp_path)
    store.import_source("daily/2026-01-01.md", "Archived source", archived=True)
    export_memory(store)
    report = health.collect_health()
    assert report.archived_daily_log_count == 1
    assert report.uncompiled_daily_logs == ["2026-01-01.md"]
    assert report.status == "attention"
