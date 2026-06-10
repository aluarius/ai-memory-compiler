"""Operational health summary for the memory compiler pipeline.

This is a cheap local "doctor" command: it runs structural KB checks, inspects
pipeline state, and reports pending operational work without making LLM calls.
"""

from __future__ import annotations

import argparse
import json
from collections import Counter, deque
from dataclasses import asdict, dataclass
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
)
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
    checks = [
        lint.check_broken_links,
        lint.check_index_consistency,
        lint.check_orphan_pages,
        lint.check_orphan_sources,
        lint.check_stale_articles,
        lint.check_missing_backlinks,
        lint.check_sparse_articles,
        lint.check_weak_connectivity,
    ]

    issues: list[dict[str, Any]] = []
    for check in checks:
        issues.extend(check())
    return issues


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
    pending_flush_count: int,
    uncompiled_count: int,
    stale_count: int,
    compile_status: str,
) -> str:
    if issue_counts.errors > 0:
        return "unhealthy"
    if failed_flush_count or pending_flush_count or uncompiled_count or stale_count:
        return "attention"
    if compile_status == "failed":
        return "attention"
    return "ok"


def collect_health() -> HealthReport:
    state = _read_json(STATE_FILE)
    issues = run_structural_checks()
    issue_counts = _issue_counts(issues)
    failed_flushes = _failed_flush_contexts()
    pending_flushes = _pending_flush_contexts()
    uncompiled = _uncompiled_daily_logs(state)
    stale = _stale_daily_logs(state)
    last_compile = _last_compile_status(_tail_lines(COMPILE_LOG_FILE))
    last_lint = state.get("last_lint")

    status = _overall_status(
        issue_counts=issue_counts,
        failed_flush_count=len(failed_flushes),
        pending_flush_count=len(pending_flushes),
        uncompiled_count=len(uncompiled),
        stale_count=len(stale),
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
        permanent_failed_contexts=_relative_names(_permanent_failed_contexts(), REPORTS_DIR),
        pending_flush_contexts=_relative_names(pending_flushes, SCRIPTS_DIR),
        last_compile=last_compile,
        last_flush_line=_last_flush_line(_tail_lines(FLUSH_LOG_FILE, limit=50)),
        last_lint=last_lint if isinstance(last_lint, str) else None,
        total_cost=float(state.get("total_cost") or 0.0),
        runtime_config=load_runtime_config(),
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
        f"- query: {runtimes.get('query_runtime', 'unknown')}",
        f"- lint: {runtimes.get('lint_runtime', 'unknown')}",
    ]

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
