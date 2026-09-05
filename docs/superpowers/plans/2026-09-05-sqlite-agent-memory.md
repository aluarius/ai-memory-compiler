# SQLite Agent Memory Implementation Plan

> **For agentic workers:** Follow the design and the task ownership below. Execute
> independent runtime and retrieval work with dispatching-parallel-agents while
> the controller implements and integrates storage and the write pipeline.

**Goal:** Move agent memory to a transactional local database and repair the
confirmed processing, search, and graph-performance defects.

**Architecture:** SQLite owns content and processing state. Models propose JSON
changes; Python validates and commits. Markdown is a reproducible export.

**Tech Stack:** Python 3.12+, sqlite3, PyYAML, existing Claude/Codex runtimes, pytest,
optional FastEmbed for local multilingual retrieval.

**Spec:** `docs/superpowers/specs/2026-09-05-sqlite-agent-memory-design.md`

## Global Constraints

- Preserve every legacy article and source byte during import and initial export.
- No LLM calls while a database write transaction is held.
- No destructive rollback or cleanup of the live corpus.
- Sanitized captures and checkpoints are durable before background launch.
- Agents are primary consumers; file editing is an explicit import operation.
- Code, identifiers, and comments are English; user-facing communication is Russian.
- No Git push or AI attribution.

## Task 1: Runtime reliability

Ownership: `scripts/codex_exec.py`, `scripts/runtime_config.py`, their tests, and
an optional focused process helper. Do not edit the live user's global config.

- [x] Add failing subprocess timeout and child-cleanup tests using a real sleeping
  Python subprocess and a temporary output file; assert the call terminates, the
  descendant is reaped, and the context is not included in the raised error.
- [x] Extend `run_codex_prompt(..., timeout_seconds: float = 1200)` without breaking
  existing callers. Validate positive finite deadlines, start a child process
  group, terminate/kill on expiration, reap it, and bound stderr diagnostics.
- [x] Permit an explicit resolved CLI path and a configured noninteractive model.
  Preserve the model selected by the caller. Do not silently replace it on errors.
- [x] Run `uv run python -m pytest tests/test_codex_exec.py tests/test_runtime_config.py -q`.

## Task 2: Canonical store and migration

Ownership: controller; new `scripts/memory_store.py`, `scripts/memory_migrate.py`,
`scripts/memory_export.py`, `scripts/article_schema.py`, tests, config/dependencies.

- [x] Write tests for idempotent import, exact source/article round trip, foreign
  reference validation, transaction rollback, revisions, generation conflicts,
  idempotent source appends, and exclusive job claims.
- [x] Implement `MemoryStore(root: Path, *, readonly: bool = False)` with
  `db_path = root / 'scripts/memory.sqlite'`, explicit `initialize()` and
  `is_initialized(root)`, and context-managed `connect(readonly=False)`.
- [x] Retrieval interface: `list_articles(project: str | None = None)` and
  `read_article(path: str)` return dictionaries with `path`, `title`, `summary`,
  `body`, `updated`, `sources`, `projects`, `content_hash`, and `revision`.
  `list_sources(include_archive=True)` returns `path`, `content`, `archived`,
  `content_hash`. `read_source(path)` returns text or None. `usage_counts()` returns
  path-to-count; `record_read(path)` updates counters transactionally.
- [x] Implement `commit_articles(changes, *, source_updates=None,
  expected_generation=None, build_entry=None, deletions=())`; changes contain
  `path`, `body`, `summary`, optional `projects`. Read and validate article metadata
  from YAML. Validate all affected links against the final transaction state.
- [x] Implement namespaced `get_state(namespace, default=None)` and
  `update_state(namespace, mutator)`, durable enqueue/claim/complete/fail operations,
  append-only sources, and snapshot/backup operations.
- [x] Import legacy articles, index summaries, source logs, state/usage/checkpoints,
  pending/failed contexts and build log in one recoverable migration. Mark readiness
  only after successful validation. Retain legacy data and a backup manifest.
- [x] Export atomically from one database snapshot; preserve unrelated files and
  refuse to overwrite externally modified exports without explicit force.

## Task 3: Retrieval and context

Ownership: retrieval worker; `scripts/kb_db.py`, `scripts/mcp_server.py`,
`hooks/session-start.py`, new `scripts/semantic_search.py`,
`scripts/evaluate_retrieval.py`, and matching tests/fixtures. Coordinate dependencies
with the controller; do not edit memory_store.py or pyproject.toml.

- [x] Add tests showing canonical reads survive missing/stale Markdown exports,
  project filtering works before top-K, source archives are searched, and recent
  articles cannot starve the hub budget.
- [x] Use the store's retrieval interfaces above when initialized; retain clearly
  labelled legacy read compatibility during migration, with no implicit writes.
- [x] Implement a disposable semantic index with a content/model fingerprint and
  local multilingual embeddings. Expose explicit indexing CLI and no model download
  on normal MCP queries. Combine lexical and semantic ranks deterministically.
- [x] Add a real-question fixture/evaluator reporting Hit@5, missing expected paths,
  mode/backend, latency, and failed/unavailable modes. Do not accept silent all-zero
  runs caused by nonexistent fixtures or an unavailable model.
- [x] Make SessionStart consume hook cwd, reserve hub space, and obey a UTF-8 byte
  budget while preserving complete rows. Keep internal-invocation guards.
- [x] Run focused retrieval, MCP, hook, and evaluation tests.

## Task 4: Transactional writers and processing

Ownership: controller; `scripts/compile.py`, `scripts/flush.py`, import/capture hooks,
`scripts/health.py`, `scripts/maintenance.py`, lint/consolidation/index-rewrite, tests.

- [x] Write regressions for failed validation not advancing ingestion, stale
  generations, output parse failure, and restart without a pending legacy queue.
- [x] Replace model filesystem edits with JSON change sets and one store commit.
  Keep long calls outside write transactions; export after commit.
- [x] Enqueue before transcript checkpoint advancement; deduplicate by source range
  and context hash. Preserve provider, cwd, source path, message range and timestamps.
- [x] Complete job/source writes atomically. Bound calls, retry eligible jobs, retain
  failures, and recover leases without duplicate source entries.
- [x] Route mechanical fixes, summary rewrites, and consolidation through the same
  validated store API. Build graph snapshots once per lint pass.
- [x] Surface job failures and stale exports in health; make maintenance alerts and
  exit codes reflect operational failures while accepting today's normal backlog.

## Task 5: Verification and cutover

- [x] Run the complete suite after integration and obtain independent code review.
- [x] On a copied corpus, compare 569 baseline article bodies and all source bodies,
  run export round trip, SQLite integrity checks, and lexical/hybrid evaluation.
- [x] Verify the actual service CLI/model with a bounded prompt; fix project runtime
  selection without changing unrelated global settings.
- [ ] Back up and migrate live data under the existing writer locks, confirm new
  capture and compilation work, and process the preserved backlog.
- [x] Update README, AGENTS.md and operations documentation around the final system.
- [ ] Commit the verified changes locally, integrate the working checkout, and
  report actual validation, backup location and remaining operational limits.

## Progress

- Baseline: 149 tests passed; live corpus has 569 articles and 146 daily logs.
- Approved architecture is the user's preceding review response; no further
  approval is needed for this reversible implementation and local migration.
- Storage, capture, compiler, retrieval, runtime, health and docs are implemented.
  Full suite before final spool integration: 320 passed; later focused regressions
  cover stalled queues, runtime isolation and offline semantic refresh.
- Rehearsal imported 569 articles and 146 sources; all 715 original bodies matched
  byte-for-byte. Export round trip and SQLite integrity passed. Legacy backup is
  retained in the rehearsal's reports/migration-backups directory.
- Real CLI 0.153.4 smoke and isolated-config smoke passed with gpt-6-astra.
  One copied recovery job completed; a real 7,888-byte compile batch atomically
  changed three articles. These model calls affected only the rehearsal store.
- Frozen 32-question Russian evaluation: BM25 23/32, hybrid 29/32; zero socket
  attempts during offline evaluation. Misses remain in the fixture/report.
- Independent review closed source-link, export race/crash, pending-only retirement,
  duplicate legacy handoff and late-writer cutover findings. Final hook-timeout
  spool integration, live migration/backlog drain and final verification remain.
- Final independent spool/gate review found no remaining High/Medium. Full suite
  is green at 334 tests. Runtime isolation reduces the measured single-context
  extraction to 29 seconds in the copied-store check. Ready for live cutover.
