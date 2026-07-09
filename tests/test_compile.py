from __future__ import annotations

import importlib
import sys

import pytest

import lint

compile_script = importlib.import_module("compile")


def test_run_post_compile_lint_returns_error_count(monkeypatch) -> None:
    monkeypatch.setattr(lint, "check_broken_links", lambda: [{"severity": "error"}])
    monkeypatch.setattr(lint, "check_index_consistency", lambda: [{"severity": "error"}])
    monkeypatch.setattr(lint, "check_index_hygiene", lambda: [])
    monkeypatch.setattr(lint, "check_orphan_pages", lambda: [{"severity": "warning"}])
    monkeypatch.setattr(lint, "check_sparse_articles", lambda: [])
    monkeypatch.setattr(lint, "check_weak_connectivity", lambda: [{"severity": "suggestion"}])
    monkeypatch.setattr(lint, "check_stale_articles", lambda: [])
    monkeypatch.setattr(lint, "check_missing_backlinks", lambda: [])

    assert compile_script.run_post_compile_lint() == 2


def test_run_post_compile_lint_returns_zero_without_errors(monkeypatch) -> None:
    monkeypatch.setattr(lint, "check_broken_links", lambda: [])
    monkeypatch.setattr(lint, "check_index_consistency", lambda: [])
    monkeypatch.setattr(lint, "check_index_hygiene", lambda: [])
    monkeypatch.setattr(lint, "check_orphan_pages", lambda: [{"severity": "warning"}])
    monkeypatch.setattr(lint, "check_sparse_articles", lambda: [])
    monkeypatch.setattr(lint, "check_weak_connectivity", lambda: [{"severity": "suggestion"}])
    monkeypatch.setattr(lint, "check_stale_articles", lambda: [])
    monkeypatch.setattr(lint, "check_missing_backlinks", lambda: [])

    assert compile_script.run_post_compile_lint() == 0


def test_get_compile_timeout_seconds_uses_default(monkeypatch) -> None:
    monkeypatch.delenv(compile_script.COMPILE_TIMEOUT_ENV, raising=False)
    monkeypatch.setattr(compile_script, "DEFAULT_COMPILE_TIMEOUT_SECONDS", 123)

    assert compile_script.get_compile_timeout_seconds() == 123


def test_get_compile_timeout_seconds_reads_env(monkeypatch) -> None:
    monkeypatch.setenv(compile_script.COMPILE_TIMEOUT_ENV, "45.5")

    assert compile_script.get_compile_timeout_seconds() == 45.5


def test_get_compile_timeout_seconds_rejects_invalid_env(monkeypatch) -> None:
    monkeypatch.setattr(compile_script, "DEFAULT_COMPILE_TIMEOUT_SECONDS", 123)

    monkeypatch.setenv(compile_script.COMPILE_TIMEOUT_ENV, "not-a-number")
    assert compile_script.get_compile_timeout_seconds() == 123

    monkeypatch.setenv(compile_script.COMPILE_TIMEOUT_ENV, "0")
    assert compile_script.get_compile_timeout_seconds() == 123


# ---------------------------------------------------------------------------
# kb_git wiring in main()
# ---------------------------------------------------------------------------


def _wire_main(monkeypatch, tmp_path, compile_result):
    """Patch everything main() touches; return the kb-call recorder."""
    calls: list[str] = []

    daily = tmp_path / "daily"
    daily.mkdir()
    log = daily / "2026-07-01.md"
    log.write_text("session notes", encoding="utf-8")

    monkeypatch.setattr(compile_script, "list_raw_files", lambda: [log])
    monkeypatch.setattr(compile_script, "load_state", lambda: {"ingested": {}})
    monkeypatch.setattr(compile_script, "LOCKS_DIR", tmp_path / ".locks")
    monkeypatch.setattr(compile_script, "list_wiki_articles", lambda: [])
    monkeypatch.setattr(compile_script, "run_post_compile_lint", lambda: 0)
    monkeypatch.setattr(compile_script, "archive_old_logs", lambda: None)
    monkeypatch.setattr(compile_script, "maybe_run_consolidation", lambda: None)
    monkeypatch.setattr(compile_script, "run_summary_rewrite_best_effort", lambda: None)

    async def fake_compile(path):
        calls.append(f"compile:{path.name}")
        return compile_result

    monkeypatch.setattr(compile_script, "compile_daily_log", fake_compile)

    def record(name, result=True):
        def fn(*args):
            calls.append(name if not args else f"{name}:{args[0]}")
            return result
        return fn

    for name in ("ensure_kb_repo", "recover_interrupted_compile", "kb_rollback"):
        monkeypatch.setattr(compile_script, name, record(name))
    monkeypatch.setattr(compile_script, "kb_commit", record("kb_commit"))
    monkeypatch.setattr(compile_script, "mark_inflight", record("mark_inflight", None))
    monkeypatch.setattr(compile_script, "clear_inflight", record("clear_inflight", None))
    monkeypatch.setattr(sys, "argv", ["compile.py"])
    return calls


def test_main_checkpoints_and_commits_on_success(monkeypatch, tmp_path) -> None:
    calls = _wire_main(monkeypatch, tmp_path, compile_result=0.5)

    compile_script.main()

    assert "ensure_kb_repo" in calls
    assert "recover_interrupted_compile" in calls
    idx = calls.index("compile:2026-07-01.md")
    assert "kb_commit:checkpoint before compile of 2026-07-01.md" in calls[:idx]
    assert "mark_inflight:2026-07-01.md" in calls[:idx]
    assert "kb_commit:compile 2026-07-01.md" in calls[idx:]
    assert "clear_inflight" in calls[idx:]
    assert not any(c == "kb_rollback" for c in calls)
    assert calls[-1].startswith("kb_commit:post-compile maintenance")


def test_main_rolls_back_on_failed_compile(monkeypatch, tmp_path) -> None:
    calls = _wire_main(monkeypatch, tmp_path, compile_result=None)

    with pytest.raises(SystemExit):
        compile_script.main()

    idx = calls.index("compile:2026-07-01.md")
    assert "kb_rollback" in calls[idx:]
    assert "clear_inflight" in calls[idx:]
    assert not any(c == "kb_commit:compile 2026-07-01.md" for c in calls)


def test_maybe_run_consolidation_respects_interval(monkeypatch) -> None:
    ran = []
    monkeypatch.setattr(
        compile_script, "load_state",
        lambda: {"last_consolidation": "2026-07-01T10:00:00+05:00"},
    )
    monkeypatch.setattr(
        compile_script, "_run_consolidation_pass", lambda: ran.append(True)
    )

    compile_script.maybe_run_consolidation()

    assert ran == []


def test_maybe_run_consolidation_runs_when_stale(monkeypatch) -> None:
    ran = []
    monkeypatch.setattr(
        compile_script, "load_state",
        lambda: {"last_consolidation": "2026-01-01T10:00:00+05:00"},
    )
    monkeypatch.setattr(
        compile_script, "_run_consolidation_pass", lambda: ran.append(True)
    )

    compile_script.maybe_run_consolidation()

    assert ran == [True]


# ---------------------------------------------------------------------------
# Incremental compile of append-only daily logs
# ---------------------------------------------------------------------------


def test_plan_compile_input_modes() -> None:
    from utils import data_hash

    base = b"# log\n\n### Session (10:00)\nold content\n"
    grown = base + b"\n### Session (23:00)\nnew content\n"

    # never compiled / legacy state without size -> full
    assert compile_script.plan_compile_input(grown, None) == ("full", grown)
    assert compile_script.plan_compile_input(grown, {"hash": "x"}) == ("full", grown)

    # compiled prefix intact -> incremental tail only
    prev = {"hash": data_hash(base), "size": len(base)}
    mode, tail = compile_script.plan_compile_input(grown, prev)
    assert mode == "incremental"
    assert tail == b"\n### Session (23:00)\nnew content\n"

    # retroactive edit (prefix hash mismatch) -> full recompile
    tampered = {"hash": "deadbeef00000000", "size": len(base)}
    assert compile_script.plan_compile_input(grown, tampered) == ("full", grown)

    # file did not grow -> full (selection normally filters this out)
    same = {"hash": data_hash(grown), "size": len(grown)}
    assert compile_script.plan_compile_input(grown, same) == ("full", grown)


def test_build_log_section_marks_incremental() -> None:
    full = compile_script.build_log_section("2026-07-09.md", "full")
    inc = compile_script.build_log_section("2026-07-09.md", "incremental")

    assert "2026-07-09.md" in full and "INCREMENTAL" not in full
    assert "2026-07-09.md" in inc and "INCREMENTAL" in inc
