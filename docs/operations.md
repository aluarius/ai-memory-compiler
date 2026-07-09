# Operations

This project is intentionally script-first. Use the commands below to inspect
the memory pipeline before reaching for manual log parsing.

## Health Check

Run the local doctor command:

```bash
uv run python scripts/health.py
```

The command performs only local I/O. It does not call an LLM and does not write
lint reports or mutate the knowledge base.

Use `health.py` for quick operational triage. Use `lint.py` when you need a
persisted markdown report in `reports/lint-YYYY-MM-DD.md` or the optional LLM
contradiction check.

It reports:

- article and daily-log counts;
- structural lint counts (including index hygiene);
- uncompiled or stale daily logs;
- preserved failed flush contexts in `reports/failed-flushes/`;
- permanently failed contexts in `reports/failed-flushes/permanent/`;
- pending temporary flush contexts in `scripts/`;
- the latest compile and flush log status;
- the configured runtime for `flush`, `compile`, and `lint`.

Use JSON output when another script needs to consume the status:

```bash
uv run python scripts/health.py --json
```

By default, attention items such as uncompiled daily logs or failed flush
contexts do not make the command fail. Structural lint errors return a non-zero
exit code. For automation that should fail on any attention item, use strict
mode:

```bash
uv run python scripts/health.py --strict
```

Exit codes:

- `0` means no structural errors were found.
- `1` means strict mode found attention items.
- `2` means structural lint errors were found.

## Reading The Output

`Status: ok` means the local pipeline has no obvious action items.

`Status: attention` means the knowledge base may still be structurally valid,
but there is operational work to review. Common causes are:

- daily logs waiting for compilation;
- preserved failed flush contexts (will be retried automatically — see below);
- permanently failed contexts that exceeded retry limits and need manual triage;
- pending temporary context files from an interrupted import or flush;
- the latest compile run ending in failure.

An uncompiled log for the current day is normal — it compiles after the
end-of-day window (22:00) or on the next morning's backlog pass. Logs from
past days are picked up automatically by the daytime backlog trigger (see
Compilation Triggers below).

`Status: unhealthy` means structural lint found errors. Run:

```bash
uv run python scripts/lint.py --fix
```

`--fix` repairs the mechanical classes automatically (symmetric backlinks,
index stub rows for unindexed articles, collapsed source cells) and re-checks.
Anything left over needs a human or the next compile pass.

## Failed-Flush Lifecycle

When a flush fails (SDK outage, locked keychain under launchd, etc.), its
context is preserved in `reports/failed-flushes/` — one file per session; a
newer failure replaces older snapshots of the same session. Recovery is
layered:

1. **In-process retries** — every flush attempts up to 4 times with
   3s/30s/180s backoff before preserving the context.
2. **Opportunistic drain** — after every *successful* flush, up to 2 preserved
   sessions are retried (the environment just proved the SDK works). Sessions
   attempted within the last 6 hours are skipped (cooldown), so a burst of
   flushes can't burn through the retry budget during one outage.
3. **Nightly drain** — `scripts/maintenance.py` (launchd, 04:30) runs
   `flush.py --retry-failed`, which bypasses the cooldown.
4. **Permanent quarantine** — after 3 unsuccessful drain attempts (tracked in
   `reports/failed-flushes/retry-state.json`), a session's contexts move to
   `reports/failed-flushes/permanent/` and stop being retried. `health.py`
   reports these; review and delete them manually.

Manual drain at any time:

```bash
uv run python scripts/flush.py --retry-failed
```

Concurrency note: all LLM flush calls are serialized through
`scripts/.locks/flush-llm.lock`. Concurrent bundled-CLI instances crash each
other; the lock makes bursts of session-end hooks queue instead of failing.

## Knowledge Base Versioning

`knowledge/` contains a nested git repository (auto-created on the first
compile after this feature landed; it is invisible to the outer repo because
`knowledge/` is gitignored). The compile loop uses it for crash safety:

- before each log: `checkpoint before compile of <log>` commit captures any
  outside changes (lint fixes, manual edits);
- an inflight marker (`scripts/.locks/compile-inflight.json`) is written
  before the LLM call and cleared after;
- on a failed compile the run does `git reset --hard` + `clean -fd`, so the
  failed log stays uncompiled and recompiles cleanly next trigger;
- after a kill -9, the next compile run sees the stale marker and rolls back
  the partial writes automatically — no manual reconciliation pass needed;
  if the dead run had already recorded the log as compiled in
  `scripts/state.json` (killed between its state update and its kb commit),
  the recovery drops that entry so the rolled-back content recompiles;
- on success: `compile <log>` commit, then one `post-compile maintenance`
  commit for lint fixes / summary rewrite / archive-ref updates.

Audit what any compile actually changed:

```bash
git -C knowledge log --oneline
git -C knowledge show <commit>
```

Caveat: a rollback after a kill -9 also discards mechanical lint fixes made
between the crash and the next compile; they are regenerated by the next
`lint.py --fix` pass.

## Index Summary Rewrite

The compiler tends to append history to `knowledge/index.md` summaries
instead of rewriting them; auto-stub rows also linger. `index_rewrite.py`
detects both mechanically (rows over 200 chars, rows with the auto-stub
marker) and rewrites them with batched single-turn LLM calls. The LLM returns
plain `target: summary` lines; the script validates and applies them
deterministically — malformed lines are dropped, never written.

It runs automatically post-compile (best-effort: failures never fail the
compile) and in nightly maintenance. Manual:

```bash
uv run python scripts/index_rewrite.py --dry-run   # list targets, free
uv run python scripts/index_rewrite.py             # rewrite (LLM)
```

## Consolidation Pass

`consolidate.py` is the monthly "sleep" phase: it folds thin articles into
hub articles so the KB stays dense as it approaches the ~500-article scale
ceiling of index-based retrieval.

- Candidates: articles under 200 words whose index row is older than 14 days
  (capped at 15 per pass).
- The LLM agent merges candidate content into hubs and records deletions in
  `reports/consolidate-manifest.txt` (`DELETE concepts/x` lines); it never
  deletes files itself.
- The script applies the manifest with a path allowlist and an inbound-link
  guard, then runs structural checks; on errors the whole pass is rolled back
  via the knowledge git repo.
- Success is recorded as `last_consolidation` in `scripts/state.json`; the
  next compile after 30 days triggers the next pass (post-compile, so it runs
  in active hours with an unlocked keychain — not from 04:30 maintenance).

Manual:

```bash
uv run python scripts/consolidate.py --dry-run   # list fold candidates, free
uv run python scripts/consolidate.py             # run a pass (LLM, ~compile cost)
```

## Compilation Triggers

`compile.py` runs in three ways:

- **End-of-day** — a successful flush after 22:00 triggers a full compile,
  including today's log (original behavior). Debounced: if today's log was
  compiled less than 30 minutes ago, the trigger is skipped and the appended
  tail waits for the next flush outside the window or the morning backlog
  (an active evening otherwise recompiles the day on every flush).
- **Daytime backlog** — a successful flush at any hour triggers
  `compile.py --skip-today` when logs from *past* days are uncompiled or
  stale. This closes the gap where days whose last session ended before
  22:00 never compiled.
- **Manual** — `uv run python scripts/compile.py` (`--dry-run` to preview,
  `--skip-today` to leave the growing log alone, `--all` to force).

Recompiles of a grown log are **incremental**: daily logs are append-only, so
when the previously compiled content is an exact prefix of the current file
(verified by hash + stored `size` in `scripts/state.json`), only the appended
tail goes to the LLM, with an explicit incremental marker in the prompt. This
matters on active evenings — every post-22:00 flush triggers a compile, and
before this change one day was fully recompiled 8 times (~$4.5 each). Any
retroactive edit of already-compiled content falls back to a full recompile.

Each single-log LLM compile is bounded by `MEMORY_COMPILE_TIMEOUT_SECONDS`
(default: 1200 seconds). Lower it for debugging a stuck Claude SDK stream, for
example `MEMORY_COMPILE_TIMEOUT_SECONDS=300 uv run python scripts/compile.py`.

## Scheduled Maintenance

`scripts/maintenance.py` runs nightly at 04:30 via launchd
(`docs/launchd-maintenance.plist`, installed to `~/Library/LaunchAgents/`).
The pass: drain failed flushes → `lint.py --fix` → full lint with the LLM
contradiction check on Sundays → `health.py`, with a macOS notification when
health is not ok. Logs to `scripts/maintenance.log`.

Caveat: at 04:30 the machine may be locked; the bundled CLI then cannot reach
the keychain and LLM steps fail harmlessly (the opportunistic drain covers
recovery during active hours). The local-only steps (lint --fix, health) are
unaffected.

## Session-Start Context Budget

The SessionStart hook injects at most ~9.5KB: today's date, the tail of the
most recent daily log, then a tiered slice of the knowledge index (articles
updated in the last 14 days + most-compiled hub articles). The budget stays
under Claude Code's ~10KB hook-output threshold — larger payloads get
persisted to a file instead of inlined, which defeats the purpose. Everything
not in the slice is reachable via the `knowledge-base` MCP tools
(`search_knowledge`, `read_article`, `list_articles`, `search_daily_logs`).

Hub selection also weighs `scripts/usage.json` — read counters written by the
MCP server's `read_article`. Articles actually read twice or more qualify as
hubs even with a single compiled source, and frequently-read articles rank
first. Delete the file to reset the counters; it is regenerated on the next
read.

## Recommended Manual Loop

Usually nothing is needed — automation covers the routine. When checking in:

1. Run `uv run python scripts/health.py`.
2. If permanently failed contexts appear, review
   `reports/failed-flushes/permanent/` and delete after triage.
3. If structural errors appear, run `uv run python scripts/lint.py --fix`.
4. If past-day logs stay uncompiled across days, check `scripts/compile.log`
   for a failing compile.
