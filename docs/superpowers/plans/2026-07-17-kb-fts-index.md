# KB FTS Index Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:executing-plans. Steps use checkbox syntax.

**Goal:** SQLite+FTS5 sidecar index over the markdown KB: BM25 search for MCP, and a tiered index slice for the compile prompt (33k → ~7k fixed tokens), with fallbacks everywhere.

**Architecture:** Markdown stays the source of truth; `scripts/kb-index.sqlite` is derived and disposable (rebuilt post-compile and in maintenance, WAL + busy_timeout for the long-lived MCP reader). Any DB failure degrades to the current behavior: linear scan for search, full index for compile.

**Tech Stack:** stdlib sqlite3 (FTS5, unicode61 tokenizer — verified working with Russian), existing utils/runtime_config plumbing.

## Global Constraints

- `uv run python -m pytest` for tests; `from __future__ import annotations`; LLM untouched here.
- DB file gitignored; never inside `knowledge/` (keeps kb-git pure).
- Fallback contract: kb_db failures must never break MCP tools or a compile run.
- Conventional commits, no AI attribution.

---

### Task 1: `scripts/kb_db.py` + tests

**Interfaces (produces):**
- `DB_FILE = SCRIPTS_DIR / "kb-index.sqlite"`; module constants `KNOWLEDGE_DIR`, `INDEX_FILE` (from config) monkeypatchable.
- `rebuild_index(db_path: Path | None = None) -> int` — full rebuild in one transaction (DELETE+INSERT, WAL), returns article count. Reads summaries/updated from index.md rows (`utils.INDEX_ROW_RE`), titles/bodies from article files.
- `search(query: str, limit: int = 10, db_path: Path | None = None) -> list[dict] | None` — BM25 (title×10, summary×5, body×1), `{path,title,summary,updated,snippet}`; `None` when DB missing/broken (caller falls back); `[]` when no matches. Query sanitized to word tokens joined with OR.
- `compile_index_slice(log_text: str, db_path=None, recent_days=14, max_recent=60, max_candidates=40, max_hubs=15) -> str | None` — markdown table: recent rows + FTS candidates from log_text term profile + top hub rows by source count; includes pointer to full index + anti-duplication instruction. `None` on any failure.
- CLI: `uv run python scripts/kb_db.py rebuild` / `search "query"`.

**Steps:** RED tests (rebuild+search roundtrip incl. Russian; title-over-body ranking; punctuation-safe queries; missing-DB → None; slice contains recent+matched+pointer; slice None without DB) → implement → green → commit `feat: sqlite fts5 sidecar index for the knowledge base`.

### Task 2: MCP search on FTS

**Files:** `scripts/mcp_server.py`, `tests/test_mcp_server.py`
- `search_knowledge`: try `kb_db.search(query)`; `None` → `_legacy_search(query)` (extracted current implementation, unchanged); `[]` → "No articles matching". FTS results formatted with snippet.
- Tests: FTS path (tmp DB), fallback path (no DB), legacy formatting preserved.
- Commit `feat(mcp): BM25 search via kb index with legacy fallback`.

### Task 3: tiered compile index view

**Files:** `scripts/compile.py`, `scripts/runtime_config.py`, `scripts/maintenance.py`, `.gitignore`, tests
- runtime_config: `"compile_index_mode": "tiered"` in defaults + `get_compile_index_mode()` (values `tiered|full`; invalid → `tiered`... explicit: invalid → `full`? choose safe: invalid → `tiered` is the default path; validate against {"tiered","full"} else default).
- compile.py: `get_index_view(log_content) -> str`: mode `tiered` → `kb_db.compile_index_slice(...)` or fallback `read_wiki_index()` with warning; mode `full` → `read_wiki_index()`. Used in prompt instead of direct `read_wiki_index()`.
- Rebuild trigger: in `main()` after the post-compile `kb_commit`, best-effort `kb_db.rebuild_index()`.
- maintenance.py: `run_step("kb-index", uv + [kb_db.py, "rebuild"])` after lint-fix.
- `.gitignore`: `scripts/kb-index.sqlite*` (WAL/SHM files too).
- Tests: get_index_view mode routing + fallback-on-exception; wiring test patch additions.
- Commit `feat(compile): tiered FTS-backed index slice in compile prompt`.

### Task 4: bootstrap + live verification + docs

- `kb_db.py rebuild` on real KB (expect ~409); smoke `search` quality on 2-3 real queries.
- Tonight's uncompiled `2026-07-17.md`: run `scripts/compile.py` manually → verify tiered slice in effect, compare Cost/Tokens line vs yesterday's $12.58 full-index run.
- operations.md: KB Index section (derived DB, rebuild points, fallback, `compile_index_mode` revert knob). Memory update.
- Commit `docs: kb index operations`, push all.
