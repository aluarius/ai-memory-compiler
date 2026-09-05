from __future__ import annotations

import argparse
import asyncio
import importlib
import importlib.util
import io
import json
import sys
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path
from threading import Event
from types import SimpleNamespace

import pytest

import import_session
import migration_gate
from locking import file_lock
from memory_store import MemoryStore


def test_waiting_writer_selects_canonical_after_database_publication(tmp_path: Path) -> None:
    started = Event()
    seen = []
    @migration_gate.guard_legacy_writer(lambda: tmp_path)
    def writer() -> None:
        seen.append(MemoryStore.is_initialized(tmp_path))
        if not seen[-1]:
            (tmp_path / "late-legacy-write.md").write_text("late write")
    def blocked_writer() -> None:
        started.set()
        writer()
    with ThreadPoolExecutor(max_workers=1) as pool:
        with file_lock(tmp_path / "scripts/.locks/migration.lock"):
            future = pool.submit(blocked_writer)
            assert started.wait(1)
            assert not future.done()
            MemoryStore(tmp_path).initialize()
        future.result(timeout=2)
    assert seen == [True]
    assert not (tmp_path / "late-legacy-write.md").exists()


@pytest.mark.parametrize("canonical", [False, True])
def test_nested_async_maintenance_uses_one_gate_without_lock_inversion(tmp_path, monkeypatch, canonical) -> None:
    if canonical:
        MemoryStore(tmp_path).initialize()
    gates = []
    @contextmanager
    def counted(path: Path):
        gates.append(path)
        with file_lock(path):
            yield
    monkeypatch.setattr(migration_gate, "file_lock", counted)
    @migration_gate.guard_legacy_writer(lambda: tmp_path)
    async def nested() -> str:
        with migration_gate.writer_gate(tmp_path) as actual:
            assert actual is canonical
        return "done"
    @migration_gate.guard_legacy_writer(lambda: tmp_path)
    def outer() -> str:
        with file_lock(tmp_path / "scripts/.locks/compile.lock"):
            return asyncio.run(nested())
    assert outer() == "done"
    assert gates == [tmp_path.resolve() / "scripts/.locks/migration.lock"]


def test_canonical_work_releases_gate_before_waiting_for_model(tmp_path: Path) -> None:
    MemoryStore(tmp_path).initialize()
    running, finish = Event(), Event()
    @migration_gate.guard_legacy_writer(lambda: tmp_path)
    def writer() -> None:
        running.set()
        assert finish.wait(2)
    def can_acquire() -> bool:
        with file_lock(tmp_path / "scripts/.locks/migration.lock"):
            return True
    with ThreadPoolExecutor(max_workers=2) as pool:
        future = pool.submit(writer)
        try:
            assert running.wait(1)
            assert pool.submit(can_acquire).result(timeout=1)
        finally:
            finish.set()
        future.result(timeout=2)


def test_legacy_action_keeps_migration_out_until_context_is_durable(tmp_path: Path) -> None:
    running, finish = Event(), Event()
    @migration_gate.guard_legacy_writer(lambda: tmp_path)
    def writer() -> None:
        running.set()
        assert finish.wait(2)
        (tmp_path / "context.md").write_text("durable context")
    def publish() -> None:
        with file_lock(tmp_path / "scripts/.locks/migration.lock"):
            assert (tmp_path / "context.md").read_text() == "durable context"
            MemoryStore(tmp_path).initialize()
    with ThreadPoolExecutor(max_workers=2) as pool:
        future = pool.submit(writer)
        assert running.wait(1)
        migration = pool.submit(publish)
        assert not migration.done()
        finish.set()
        future.result(timeout=2)
        migration.result(timeout=2)


def _load_hook(name: str, monkeypatch):
    monkeypatch.delenv("CLAUDE_INVOKED_BY", raising=False)
    monkeypatch.delenv("MEMORY_COMPILER_INTERNAL", raising=False)
    path = Path(__file__).resolve().parents[1] / "hooks" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name.replace("-", "_"), path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("name", ["flush", "compile", "consolidate", "index_rewrite", "lint"])
def test_actual_entry_points_recheck_backend_after_gate(tmp_path: Path, monkeypatch, name: str) -> None:
    root = tmp_path
    hooks = {"codex-stop", "pre-compact", "session-end"}
    module = _load_hook(name, monkeypatch) if name in hooks else importlib.import_module(name)
    for attribute, value in {
        "ROOT": root, "ROOT_DIR": root, "SCRIPTS_DIR": root / "scripts",
        "KNOWLEDGE_DIR": root / "knowledge", "REPORTS_DIR": root / "reports",
        "LOCKS_DIR": root / "scripts/.locks", "INDEX_FILE": root / "knowledge/index.md",
    }.items():
        if hasattr(module, attribute):
            monkeypatch.setattr(module, attribute, value)
    observed = []
    async def canonical_call(*args, **kwargs):
        observed.append("canonical")
        return False if name == "consolidate" else 0
    if name == "flush":
        import flush_service
        monkeypatch.setattr(flush_service, "main", lambda *args: observed.append("canonical") or 0)
        monkeypatch.setattr(module, "parse_args", lambda: pytest.fail("Legacy parser was selected"))
    elif name in {"compile", "consolidate", "index_rewrite"}:
        import compiler_service
        target = {"compile": "run_compile", "consolidate": "consolidate_articles", "index_rewrite": "rewrite_summaries"}[name]
        monkeypatch.setattr(compiler_service, target, canonical_call)
        if name == "consolidate":
            monkeypatch.setattr(module, "select_candidates", lambda: [])
        monkeypatch.setattr(sys, "argv", [f"{name}.py"])
    elif name == "lint":
        monkeypatch.setattr(module.sys, "argv", ["lint.py", "--structural-only"])
        monkeypatch.setattr(module, "list_wiki_articles", lambda: pytest.fail("Legacy graph was selected"))
    else:
        import capture_service
        transcript = root / "session.jsonl"
        transcript.write_text("{}\n")
        payload = {"session_id": "s1", "transcript_path": str(transcript)}
        monkeypatch.setattr(module.sys, "stdin", io.StringIO(json.dumps(payload)))
        monkeypatch.setattr(capture_service, "capture_transcript", lambda *args, **kwargs: observed.append("canonical") or [])
        monkeypatch.setattr(capture_service, "spawn_worker", lambda *args: True)
    started = Event()
    def invoke():
        started.set()
        return module.main()
    with ThreadPoolExecutor(max_workers=1) as pool:
        with file_lock(root / "scripts/.locks/migration.lock"):
            future = pool.submit(invoke)
            assert started.wait(1)
            assert not future.done()
            MemoryStore(root).initialize()
        future.result(timeout=2)
    if name == "lint":
        assert MemoryStore(root).get_state("pipeline")["last_lint"]
    else:
        assert observed == ["canonical"]


def test_importer_releases_gate_before_waiting_child_and_preserves_context_after_cutover(tmp_path, monkeypatch) -> None:
    transcript = tmp_path / "source.md"
    transcript.write_text("Important conversation")
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    failed = tmp_path / "failed"
    monkeypatch.setattr(import_session, "ROOT_DIR", tmp_path)
    monkeypatch.setattr(import_session, "SCRIPTS_DIR", scripts)
    monkeypatch.setattr(import_session, "FAILED_FLUSH_DIR", failed)
    monkeypatch.setattr(import_session, "parse_args", lambda: argparse.Namespace(
        transcript=transcript, session_id="s1", agent="codex", provider="openai",
        model=None, cwd=None, source="test", after_message_count=0, until_message_count=None,
    ))
    captured = []
    def run_child(command, **kwargs):
        context = Path(command[2])
        assert context.exists()
        def migrate_during_child_wait():
            with file_lock(tmp_path / "scripts/.locks/migration.lock"):
                store = MemoryStore(tmp_path)
                store.initialize()
                store.enqueue(context.read_text(), {}, identity="migrated")
        with ThreadPoolExecutor(max_workers=1) as pool:
            pool.submit(migrate_during_child_wait).result(timeout=2)
        captured.append(context)
        return SimpleNamespace(returncode=1)
    monkeypatch.setattr(import_session.subprocess, "run", run_child)
    assert import_session.main() == 1
    assert captured[0].exists()
    assert not failed.exists()
    assert MemoryStore(tmp_path).jobs()[0]["context"] == "Important conversation"
