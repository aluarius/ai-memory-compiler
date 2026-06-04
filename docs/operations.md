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
- structural lint counts;
- uncompiled or stale daily logs;
- preserved failed flush contexts in `reports/failed-flushes/`;
- pending temporary flush contexts in `scripts/`;
- the latest compile and flush log status;
- the configured runtime for `flush`, `compile`, `query`, and `lint`.

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
- preserved failed flush contexts;
- pending temporary context files from an interrupted import or flush;
- the latest compile run ending in failure.

An uncompiled log for the current day can be normal before the end-of-day
compile window. Older uncompiled logs usually need manual review.

`Status: unhealthy` means structural lint found errors. Run the full structural
lint command to get the persisted report:

```bash
uv run python scripts/lint.py --structural-only
```

## Recommended Maintenance Loop

1. Run `uv run python scripts/health.py`.
2. If daily logs are uncompiled, run `uv run python scripts/compile.py --dry-run`.
3. If structural errors appear, run `uv run python scripts/lint.py --structural-only`.
4. If failed flush contexts appear, inspect `reports/failed-flushes/`. Retry
   automation is planned as the next maintenance layer.
