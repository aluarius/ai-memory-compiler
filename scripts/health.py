"""Operational health summary for the memory compiler pipeline.

This is a cheap local "doctor" command: it runs structural KB checks, inspects
pipeline state, and reports pending operational work without making LLM calls.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import time
from collections import Counter, deque
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import lint
from config import (
    DAILY_ARCHIVE_DIR,
    DAILY_DIR,
    KNOWLEDGE_DIR,
    REPORTS_DIR,
    SCRIPTS_DIR,
    STATE_FILE,
    today_iso,
)
from memory_export import snapshot_files
from memory_store import MemoryStore, content_hash
from runtime_config import load_runtime_config
from utils import file_hash

FAILED_FLUSH_DIR = REPORTS_DIR / "failed-flushes"
COMPILE_LOG_FILE = SCRIPTS_DIR / "compile.log"
FLUSH_LOG_FILE = SCRIPTS_DIR / "flush.log"
TEMP_CONTEXT_PATTERNS = ("session-flush-*.md", "flush-context-*.md", "import-flush-*.md")


@dataclass(frozen=True)
class IssueCounts:
    total: int
    errors: int
    warnings: int
    suggestions: int


@dataclass(frozen=True)
class PipelineLogStatus:
    status: str
    detail: str | None


@dataclass(frozen=True)
class HealthReport:
    status: str
    issue_counts: IssueCounts
    article_count: int
    daily_log_count: int
    archived_daily_log_count: int
    uncompiled_daily_logs: list[str]
    stale_daily_logs: list[str]
    failed_flush_contexts: list[str]
    permanent_failed_contexts: list[str]
    pending_flush_contexts: list[str]
    last_compile: PipelineLogStatus
    last_flush_line: str | None
    last_lint: str | None
    total_cost: float
    runtime_config: dict[str, Any]
    backend: str = "legacy"
    database_errors: list[str] = field(default_factory=list)
    job_counts: dict[str, int] = field(default_factory=dict)
    operational_jobs: list[dict[str, Any]] = field(default_factory=list)
    export_drift: list[str] = field(default_factory=list)
    capture_spool_files: list[str] = field(default_factory=list)


def _read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    return data if isinstance(data, dict) else {}


def _article_count() -> int:
    count = 0
    for subdir_name in ("concepts", "connections", "qa"):
        subdir = KNOWLEDGE_DIR / subdir_name
        if subdir.exists():
            count += len(list(subdir.glob("*.md")))
    return count


def _daily_logs(directory: Path) -> list[Path]:
    if not directory.exists():
        return []
    return sorted(directory.glob("*.md"))


def _relative_names(paths: list[Path], root: Path) -> list[str]:
    names = []
    for path in sorted(paths):
        try:
            names.append(str(path.relative_to(root)).replace("\\", "/"))
        except ValueError:
            names.append(str(path))
    return names


def _pending_flush_contexts() -> list[Path]:
    found: list[Path] = []
    for pattern in TEMP_CONTEXT_PATTERNS:
        found.extend(SCRIPTS_DIR.glob(pattern))
    return sorted({path for path in found if path.is_file()})


def _failed_flush_contexts() -> list[Path]:
    if not FAILED_FLUSH_DIR.exists():
        return []
    return sorted(path for path in FAILED_FLUSH_DIR.glob("*.md") if path.is_file())


def _permanent_failed_contexts() -> list[Path]:
    permanent_dir = FAILED_FLUSH_DIR / "permanent"
    if not permanent_dir.exists():
        return []
    return sorted(path for path in permanent_dir.glob("*.md") if path.is_file())


def _uncompiled_daily_logs(state: dict[str, Any]) -> list[str]:
    ingested = state.get("ingested", {})
    if not isinstance(ingested, dict):
        ingested = {}
    return [path.name for path in _daily_logs(DAILY_DIR) if path.name not in ingested]


def _stale_daily_logs(state: dict[str, Any]) -> list[str]:
    ingested = state.get("ingested", {})
    if not isinstance(ingested, dict):
        return []

    stale = []
    for path in _daily_logs(DAILY_DIR):
        metadata = ingested.get(path.name)
        if not isinstance(metadata, dict):
            continue
        if metadata.get("hash") != file_hash(path):
            stale.append(path.name)
    return stale


def _tail_lines(path: Path, limit: int = 200) -> list[str]:
    if not path.exists():
        return []
    lines: deque[str] = deque(maxlen=limit)
    try:
        with open(path, encoding="utf-8") as file:
            for line in file:
                stripped = line.rstrip()
                if stripped:
                    lines.append(stripped)
    except OSError:
        return []
    return list(lines)


def _last_compile_status(lines: list[str]) -> PipelineLogStatus:
    if not lines:
        return PipelineLogStatus(status="missing", detail=None)

    status = "unknown"
    status_index: int | None = None
    for index, line in enumerate(lines):
        if "Compilation failed" in line:
            status = "failed"
            status_index = index
        elif "Compilation complete" in line:
            status = "complete"
            status_index = index
        elif "Nothing to compile" in line:
            status = "up_to_date"
            status_index = index

    if status_index is None:
        return PipelineLogStatus(status=status, detail=lines[-1])

    detail = lines[status_index]
    if status == "failed":
        for line in lines[status_index + 1 :]:
            if line.startswith("Failed logs:"):
                detail = f"{detail}; {line}"
                break
    return PipelineLogStatus(status=status, detail=detail)


def _last_flush_line(lines: list[str]) -> str | None:
    return lines[-1] if lines else None


def run_structural_checks() -> list[dict[str, Any]]:
    """Run local structural checks without the LLM contradiction pass."""
    return lint.structural_checks()


def _issue_counts(issues: list[dict[str, Any]]) -> IssueCounts:
    by_severity = Counter(str(issue.get("severity", "")) for issue in issues)
    return IssueCounts(
        total=len(issues),
        errors=by_severity["error"],
        warnings=by_severity["warning"],
        suggestions=by_severity["suggestion"],
    )


def _overall_status(
    *,
    issue_counts: IssueCounts,
    failed_flush_count: int,
    permanent_failed_count: int,
    pending_flush_count: int,
    uncompiled_count: int,
    stale_count: int,
    compile_status: str,
) -> str:
    if issue_counts.errors > 0:
        return "unhealthy"
    if failed_flush_count or pending_flush_count or uncompiled_count or stale_count:
        return "attention"
    if permanent_failed_count:
        # Exceeded retry limits — these never resolve without manual triage.
        return "attention"
    if compile_status == "failed":
        return "attention"
    return "ok"


def collect_health() -> HealthReport:
    # Existence, rather than a permissive probe, keeps corrupt canonical storage
    # from silently selecting stale Markdown and JSON state.
    store = MemoryStore(KNOWLEDGE_DIR.parent, readonly=True)
    if store.db_path.exists():
        return _collect_canonical_health(store)
    state = _read_json(STATE_FILE)
    issues = run_structural_checks()
    issue_counts = _issue_counts(issues)
    failed_flushes = _failed_flush_contexts()
    permanent_failed = _permanent_failed_contexts()
    pending_flushes = _pending_flush_contexts()
    uncompiled = _uncompiled_daily_logs(state)
    stale = _stale_daily_logs(state)
    last_compile = _last_compile_status(_tail_lines(COMPILE_LOG_FILE))
    last_lint = state.get("last_lint")

    status = _overall_status(
        issue_counts=issue_counts,
        failed_flush_count=len(failed_flushes),
        permanent_failed_count=len(permanent_failed),
        pending_flush_count=len(pending_flushes),
        uncompiled_count=sum(name != f"{today_iso()}.md" for name in uncompiled),
        stale_count=sum(name != f"{today_iso()}.md" for name in stale),
        compile_status=last_compile.status,
    )

    return HealthReport(
        status=status,
        issue_counts=issue_counts,
        article_count=_article_count(),
        daily_log_count=len(_daily_logs(DAILY_DIR)),
        archived_daily_log_count=len(_daily_logs(DAILY_ARCHIVE_DIR)),
        uncompiled_daily_logs=uncompiled,
        stale_daily_logs=stale,
        failed_flush_contexts=_relative_names(failed_flushes, REPORTS_DIR),
        permanent_failed_contexts=_relative_names(permanent_failed, REPORTS_DIR),
        pending_flush_contexts=_relative_names(pending_flushes, SCRIPTS_DIR),
        last_compile=last_compile,
        last_flush_line=_last_flush_line(_tail_lines(FLUSH_LOG_FILE, limit=50)),
        last_lint=last_lint if isinstance(last_lint, str) else None,
        total_cost=float(state.get("total_cost") or 0.0),
        runtime_config=load_runtime_config(),
    )


def _export_drift(root: Path, snapshot: dict) -> list[str]:
    """Compare the manifest and owned files with current source and article bytes."""
    expected = {path: content_hash(body) for path, body in snapshot_files(snapshot).items()}
    exported = snapshot["export_state"]
    previous = exported.get("files", {})
    drift: list[str] = []
    if exported.get("generation") != snapshot["generation"]:
        drift.append(f"Generation: exported {exported.get('generation', 'never')}, canonical {snapshot['generation']}")
    for relative in sorted(expected.keys() | previous.keys()):
        path = root / relative
        if path.is_symlink() or not path.resolve().is_relative_to(root):
            drift.append(f"Unsafe export path: {relative}")
            continue
        if relative not in expected:
            drift.append(f"Retired export pending removal: {relative}")
            continue
        if previous.get(relative) != expected[relative]:
            drift.append(f"Manifest stale: {relative}")
        try:
            actual = content_hash(path.read_bytes().decode("utf-8"))
        except FileNotFoundError:
            drift.append(f"Export missing: {relative}")
        except (OSError, UnicodeError) as exc:
            drift.append(f"Export unreadable: {relative}: {exc}")
        else:
            if actual != expected[relative]:
                detail = "stale" if actual == previous.get(relative) else "modified"
                drift.append(f"Export {detail}: {relative}")
    return drift


def _collect_canonical_health(store: MemoryStore) -> HealthReport:
    """Inspect canonical state without writes, model calls, or legacy-state fallback."""
    snapshot: dict | None = None
    jobs: list[dict] = []
    database_errors: list[str] = []
    issues: list[dict] = []
    try:
        MemoryStore.is_initialized(store.root)
        database_errors = store.integrity_check()
        if not database_errors:
            snapshot = store.snapshot()
            jobs = store.jobs(("pending", "running", "failed", "quarantined"))
            issues = lint.structural_checks(snapshot)
    except (sqlite3.Error, ValueError, OSError) as exc:
        database_errors.append(str(exc))
    issues.extend({"severity": "error", "check": "database", "detail": error} for error in database_errors)
    counts = _issue_counts(issues)
    pipeline = snapshot["pipeline"] if snapshot is not None else {}
    sources = snapshot["sources"] if snapshot is not None else []
    uncompiled, stale = lint.source_backlog(snapshot) if snapshot is not None else ([], [])
    job_counts = dict(Counter(job["status"] for job in jobs))
    expired = [job for job in jobs if job["status"] == "running" and (job["lease_until"] or 0) <= time.time()]
    job_counts["expired"] = len(expired)
    expired_ids = {job["id"] for job in expired}
    stalled_ids = set()
    for job in jobs:
        if job["status"] == "pending":
            try:
                age = time.time() - datetime.fromisoformat(job["created"]).timestamp()
            except (ValueError, TypeError):
                age = float("inf")
            if age > 3600:
                stalled_ids.add(job["id"])
    if stalled_ids:
        job_counts["stalled_pending"] = len(stalled_ids)
    operational = [job for job in jobs if job["status"] in ("failed", "quarantined") or job["id"] in expired_ids | stalled_ids]
    # Contexts and metadata may contain private source text. Only operational
    # identifiers and retained errors belong in health output.
    operational_jobs = [{key: job[key] for key in ("id", "kind", "status", "attempts", "last_error", "lease_until", "next_attempt")} for job in operational]
    drift = _export_drift(store.root, snapshot) if snapshot is not None else []
    spool_directory = store.root / "reports/capture-spool"
    spool_files = sorted(str(path.relative_to(store.root)) for path in
                         [*spool_directory.glob("*.json"), *(spool_directory / "failures").glob("*.json")])
    compile_data = pipeline.get("last_compile", {})
    if isinstance(compile_data, dict):
        last_compile = PipelineLogStatus(str(compile_data.get("status", "unknown")), compile_data.get("detail"))
    else:
        last_compile = PipelineLogStatus("unknown", None)
    status = _overall_status(
        issue_counts=counts, failed_flush_count=len(operational), permanent_failed_count=0,
        pending_flush_count=len(drift) + len(spool_files),
        uncompiled_count=sum(name != f"{today_iso()}.md" for name in uncompiled),
        stale_count=sum(name != f"{today_iso()}.md" for name in stale),
        compile_status=last_compile.status,
    )
    return HealthReport(
        status=status, issue_counts=counts,
        article_count=len(snapshot["articles"]) if snapshot is not None else 0,
        daily_log_count=sum(not source["archived"] for source in sources),
        archived_daily_log_count=sum(bool(source["archived"]) for source in sources),
        uncompiled_daily_logs=uncompiled, stale_daily_logs=stale,
        failed_flush_contexts=[], permanent_failed_contexts=[], pending_flush_contexts=[],
        last_compile=last_compile, last_flush_line=None,
        last_lint=pipeline.get("last_lint"), total_cost=float(pipeline.get("total_cost") or 0),
        runtime_config=load_runtime_config(), backend="sqlite", database_errors=database_errors,
        job_counts=job_counts, operational_jobs=operational_jobs, export_drift=drift,
        capture_spool_files=spool_files,
    )


def _format_list(items: list[str], *, max_items: int) -> list[str]:
    shown = items[:max_items]
    lines = [f"  - {item}" for item in shown]
    remaining = len(items) - len(shown)
    if remaining > 0:
        lines.append(f"  - ... and {remaining} more")
    return lines


def format_report(report: HealthReport, *, max_items: int = 8) -> str:
    runtimes = report.runtime_config
    lines = [
        "Memory Compiler Health",
        f"Status: {report.status}",
        f"Backend: {report.backend}",
        "",
        "Knowledge base",
        f"- Articles: {report.article_count}",
        (
            "- Structural lint: "
            f"{report.issue_counts.errors} errors, "
            f"{report.issue_counts.warnings} warnings, "
            f"{report.issue_counts.suggestions} suggestions"
        ),
        f"- Last lint: {report.last_lint or 'unknown'}",
        "",
        "Sources",
        f"- Active daily logs: {report.daily_log_count}",
        f"- Archived daily logs: {report.archived_daily_log_count}",
        f"- Uncompiled daily logs: {len(report.uncompiled_daily_logs)}",
        f"- Stale daily logs: {len(report.stale_daily_logs)}",
        "",
        "Flush pipeline",
        f"- Failed flush contexts: {len(report.failed_flush_contexts)}",
        f"- Permanently failed contexts: {len(report.permanent_failed_contexts)}",
        f"- Pending temp contexts: {len(report.pending_flush_contexts)}",
        f"- Last flush log line: {report.last_flush_line or 'missing'}",
        "",
        "Compile pipeline",
        f"- Last compile status: {report.last_compile.status}",
        f"- Last compile detail: {report.last_compile.detail or 'missing'}",
        f"- Total recorded cost: ${report.total_cost:.2f}",
        "",
        "Runtime config",
        f"- flush: {runtimes.get('flush_runtime', 'unknown')}",
        f"- compile: {runtimes.get('compile_runtime', 'unknown')}",
        f"- lint: {runtimes.get('lint_runtime', 'unknown')}",
    ]

    if report.backend == "sqlite":
        lines.extend(["", "Canonical storage", f"- Database integrity: {'failed' if report.database_errors else 'ok'}"])
        lines.extend(_format_list(report.database_errors, max_items=max_items))
        lines.extend([f"- Jobs: {json.dumps(report.job_counts, sort_keys=True)}", f"- Export drift: {len(report.export_drift)}"])
        lines.extend(_format_list(report.export_drift, max_items=max_items))
        if report.capture_spool_files:
            lines.append(f"- Capture spool attention: {len(report.capture_spool_files)}")
            lines.extend(_format_list(report.capture_spool_files, max_items=max_items))
        lines.extend(_format_list([
            f"{job['id']}: {job['status']}, attempts={job['attempts']}, error={job['last_error'] or 'none'}"
            for job in report.operational_jobs
        ], max_items=max_items))

    if report.failed_flush_contexts:
        lines.extend(["", "Failed flush contexts"])
        lines.extend(_format_list(report.failed_flush_contexts, max_items=max_items))

    if report.pending_flush_contexts:
        lines.extend(["", "Pending temp contexts"])
        lines.extend(_format_list(report.pending_flush_contexts, max_items=max_items))

    if report.uncompiled_daily_logs:
        lines.extend(["", "Uncompiled daily logs"])
        lines.extend(_format_list(report.uncompiled_daily_logs, max_items=max_items))

    if report.stale_daily_logs:
        lines.extend(["", "Stale daily logs"])
        lines.extend(_format_list(report.stale_daily_logs, max_items=max_items))

    lines.extend(["", "Next steps"])
    if report.operational_jobs:
        lines.append("- Run: uv run python scripts/flush.py --drain; review quarantined jobs manually.")
    if report.capture_spool_files:
        lines.append("- Run: uv run python scripts/capture_spool.py --no-flush; review capture-spool/failures against original transcripts.")
    if report.export_drift:
        lines.append("- Run: uv run python scripts/memory_export.py; review externally modified files before forcing export.")
    if report.issue_counts.errors:
        lines.append("- Run: uv run python scripts/lint.py --structural-only")
    if report.failed_flush_contexts:
        lines.append("- Run: uv run python scripts/flush.py --retry-failed")
    if report.permanent_failed_contexts:
        lines.append(
            "- Review reports/failed-flushes/permanent — these exceeded retry limits "
            "and need manual triage."
        )
    if report.pending_flush_contexts:
        lines.append(
            "- Check scripts/*.md temp contexts; they may indicate an unfinished flush/import."
        )
    if report.uncompiled_daily_logs or report.stale_daily_logs:
        lines.append("- Run: uv run python scripts/compile.py --dry-run")
    if report.status == "ok":
        lines.append("- No action needed.")

    return "\n".join(lines) + "\n"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Show operational health for the memory compiler")
    parser.add_argument("--json", action="store_true", help="Print machine-readable JSON")
    parser.add_argument(
        "--strict",
        action="store_true",
        help="Exit non-zero for attention items, not only structural errors",
    )
    parser.add_argument(
        "--max-items",
        type=int,
        default=8,
        help="Maximum file names per list in text output",
    )
    return parser.parse_args(argv)


def exit_code(report: HealthReport, *, strict: bool) -> int:
    if report.issue_counts.errors:
        return 2
    if strict and report.status != "ok":
        return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    report = collect_health()

    if args.json:
        print(json.dumps(asdict(report), indent=2))
    else:
        print(format_report(report, max_items=args.max_items), end="")

    return exit_code(report, strict=args.strict)


if __name__ == "__main__":
    raise SystemExit(main())
