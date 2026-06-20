from __future__ import annotations

import importlib

import lint

compile_script = importlib.import_module("compile")


def test_run_post_compile_lint_returns_error_count(monkeypatch) -> None:
    monkeypatch.setattr(lint, "check_broken_links", lambda: [{"severity": "error"}])
    monkeypatch.setattr(lint, "check_index_consistency", lambda: [{"severity": "error"}])
    monkeypatch.setattr(lint, "check_orphan_pages", lambda: [{"severity": "warning"}])
    monkeypatch.setattr(lint, "check_sparse_articles", lambda: [])
    monkeypatch.setattr(lint, "check_weak_connectivity", lambda: [{"severity": "suggestion"}])
    monkeypatch.setattr(lint, "check_stale_articles", lambda: [])

    assert compile_script.run_post_compile_lint() == 2


def test_run_post_compile_lint_returns_zero_without_errors(monkeypatch) -> None:
    monkeypatch.setattr(lint, "check_broken_links", lambda: [])
    monkeypatch.setattr(lint, "check_index_consistency", lambda: [])
    monkeypatch.setattr(lint, "check_orphan_pages", lambda: [{"severity": "warning"}])
    monkeypatch.setattr(lint, "check_sparse_articles", lambda: [])
    monkeypatch.setattr(lint, "check_weak_connectivity", lambda: [{"severity": "suggestion"}])
    monkeypatch.setattr(lint, "check_stale_articles", lambda: [])

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
