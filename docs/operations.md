# Operations

After migration, `scripts/memory.sqlite` owns memory and pipeline state.
Markdown, legacy JSON files and retrieval indexes are not recovery authorities.
Run these commands from the repository root.

## Start with health

```bash
uv run python scripts/health.py
uv run python scripts/health.py --json --strict
```

Health performs local checks without model calls. It reports the storage
backend, database integrity, active and archived sources, compile checkpoints,
job states, structural issues and export drift.

Exit code `2` means structural errors. With `--strict`, other attention items
return `1`; otherwise they can return `0`. Read the status and details, not
only the non-strict exit code. An unreadable canonical store must not silently
fall back to exported files.

## Runtime configuration

`scripts/runtime-config.json` selects `flush_runtime`, `compile_runtime`
and `lint_runtime` independently. Supported values are `claude` and `codex`.
For example:

```json
{
  "flush_runtime": "codex",
  "compile_runtime": "codex",
  "lint_runtime": "claude",
  "codex_bin": "/absolute/path/to/codex",
  "codex_model": null,
  "claude_model": "claude-opus-4-8"
}
```

A nonempty project `codex_bin` pin wins over `MEMORY_CODEX_BIN`.
Without a project pin, the environment override and executable discovery
provide fallbacks. Point to the actual CLI, not a desktop wrapper. This matters
for hooks, NVM installations and launchd, which may inherit a different PATH.
`MEMORY_CODEX_MODEL` overrides `codex_model`.

With a recent CLI that supports `--ignore-user-config`, the project can set
`"codex_isolate_config": true` to exclude interactive MCP/plugin configuration
and execution rules from background calls while retaining the CLI's credentials.
Set `"codex_reasoning_effort": "medium"` to avoid inheriting interactive `xhigh`.
These settings affect only this project's subprocesses. Validate the exact
binary with a bounded smoke before enabling isolation; custom provider routing
from the ignored global config will not be inherited.

Compilation model calls default to 1,200 seconds per bounded batch.
`MEMORY_COMPILE_TIMEOUT_SECONDS` accepts a positive finite number; invalid
values use the default. Flush extraction has a 1,200-second model timeout
and a longer job lease. Both Claude and Codex calls are bounded. Timeout or
authentication failures retain work for inspection and retry.

Compiler models receive read-only temporary snapshots and return the
[validated JSON contract](../AGENTS.md#model-change-set-contract).
They do not edit the live repository. Runtime credentials and billing remain
the responsibility of the selected provider; local retrieval uses neither.

## Durable capture and job recovery

Capture persists sanitized context, provenance and checkpoint advancement in
one transaction before it starts a detached worker. A failed spawn therefore
leaves a recoverable job. Workers use expiring leases; an old worker cannot
publish after another worker takes its lease.

Workers renew their own unchanged lease after a model call while still holding
the runtime lock. This preserves a completed response across machine sleep,
without repeating the model call. A changed token or completed job rejects
renewal; normal completion and failure still require a live lease.
Renewal errors from local storage do not start a provider-wide cooldown.
Cancellation unwinds without renewing the lease or replacing the cancellation
with a storage error.

On macOS, Codex model calls use a process-scoped `caffeinate -i` assertion to
prevent idle system sleep. The display can sleep, and global power settings
remain unchanged. Closing the lid or forcing sleep can still interrupt work.
Codex deadlines check both wall time and monotonic time, so suspended time or
a backwards clock adjustment cannot extend an unfinished call indefinitely.
Timeout errors retain a bounded error diagnostic with prompt echoes removed
and the shared credential redaction applied.

Provenance uses a shared scalar whitelist and secret redaction. Checkpoint keys
and import scope use an opaque hash of the original transcript identity, so
redacted identifiers cannot merge unrelated sessions. Existing checkpoint keys
upgrade atomically on their next capture. Existing jobs and source history are
not retroactively rewritten by this code update.
Pending jobs older than one hour also produce an attention status, so a failed
spawn cannot remain invisible indefinitely.
During cutover, a contended migration lock never makes a synchronous hook wait
past its deadline: it persists sanitized text to `reports/capture-spool/` and
detaches the waiting importer. Imported spool files remain under `imported/`.
Unimported spools and capture failure records appear in health output.

```bash
uv run python scripts/flush.py --drain --limit 10
uv run python scripts/flush.py --retry-failed --limit 5
```

`--drain` includes pending jobs, eligible failures and expired leases.
`--retry-failed` restricts work to eligible failures and expired leases.
Both respect retry cooldowns; their default limit is 100. A failed model call
stops the batch. Canonical processing lives in `flush_service.process_jobs`;
`flush.py` dispatches there for migrated stores.

Provider and environment failures retain the context and apply a six-hour
cooldown, including a worker-wide pause after runtime failure. They do not
automatically quarantine jobs. Repeated structurally invalid extraction can
quarantine a job after three malformed responses. A separate transactional
counter excludes provider failures. Inspect quarantined records manually;
neither normal retries nor `--force` bypass quarantine.

After fixing the cause, an explicit retry can bypass cooldown:

```bash
uv run python scripts/flush.py --retry-failed --limit 1 --force
```

Do not use force in unattended maintenance. A zero worker exit code can mean
there was no eligible work, so check health again before declaring the queue
empty. Never delete contexts or reset checkpoints to clear an alert.

Repeating `import_session.py` retries unfinished jobs from that transcript,
session and requested message range, even when its capture checkpoint already
advanced. It does not drain unrelated jobs. Cooldown, quarantine or an active
lease on unfinished requested work returns a nonzero exit code; already completed
work remains successful. Successful imports also run the normal compile trigger.

Job completion and the daily entry commit atomically. Capture time determines
the source date. Export follows separately: if it fails, the completed job
stays complete and its model response does not need to be regenerated.
Legacy recovery files remain preserved after migration, including previously
quarantined files under `reports/failed-flushes/permanent/`.

## Compilation and maintenance

```bash
uv run python scripts/compile.py --dry-run
uv run python scripts/compile.py
uv run python scripts/compile.py --skip-today
uv run python scripts/index_rewrite.py --dry-run
uv run python scripts/consolidate.py --dry-run
uv run python scripts/lint.py --structural-only
```

Compilation processes bounded source prefixes and commits articles, revisions,
build events and the exact processed-byte checkpoint together. An interrupted
later batch resumes from the committed prefix. A generation conflict rejects
the stale proposal rather than overwriting newer memory.

For existing articles, the model can return exact text replacements instead of
repeating entire bodies. Python applies them to the original snapshot and checks
the complete result. Missing, overlapping or otherwise ambiguous matches reject
the batch without advancing its checkpoint. New articles still use full bodies;
complete replacements remain supported when needed.

Summary rewriting changes metadata only. Consolidation accepts deletions only
inside its validated candidate set and preserves valid remaining references.
Neither operation delegates unrestricted file editing or Git rollback to a model.

After successful queue work, automatic compilation handles past-day backlog
during the day; after 22:00 local time it can include today's source.
A 30-minute debounce limits repeated automatic triggers.
`scripts/maintenance.py` drains the queue, performs maintenance and reports
health. Review [the launchd template](launchd-maintenance.plist) before
installing a scheduler; avoid duplicate schedules and duplicate hooks.

## Markdown export and Obsidian

```bash
uv run python scripts/memory_export.py
uv run python scripts/memory_export.py --destination /absolute/path/to/new-export
```

Export projects one consistent database snapshot into article files, a
generated catalog, a build log and daily sources. Each file replacement is
atomic; the directory as a whole is not a transaction. Interrupted exports
are retryable. Agents keep reading SQLite throughout.

Outside edits are conflicts, not automatic imports. Review and preserve them
before deciding whether to incorporate them through a validated canonical
change or replace them. Unknown files are not wholesale deleted.
Explicit `--force` preserves conflicting content under
`reports/export-conflicts/`; replaced and retired content has recovery
history under `reports/export-revisions/` and `reports/retired-exports/`.

Obsidian is an optional export reader. Editing its vault does not update
canonical memory and can make the next export stop for conflict review.
Use a separate export directory if that separation is easier to maintain.

## Retrieval and model cache

BM25 reads canonical article records and requires no model. Hybrid retrieval
adds local `intfloat/multilingual-e5-small` embeddings through optional
FastEmbed. The installed FastEmbed integration registers the official small
ONNX model explicitly; it does not substitute a third-party fork.

```bash
uv sync --extra semantic
uv run --extra semantic python scripts/semantic_search.py index
uv run --extra semantic python scripts/evaluate_retrieval.py
uv run python scripts/evaluate_retrieval.py --mode bm25
```

Only the explicit index command can download model files.
`scripts/.models/` stores the cache; ordinary queries use local files only.
`scripts/semantic-index.sqlite` is a separate disposable SQLite database.
Content and model fingerprints reject stale vectors, and reindexing reuses
unchanged embeddings.
Once explicitly enabled by creating the semantic index, successful compilation
refreshes it from the local cache. Maintenance also runs an offline refresh.
Neither automatic refresh downloads a model; unavailable semantics remain a
visible lexical fallback rather than blocking canonical commits.

MCP hybrid search reports a lexical fallback if the cache or index is missing
or stale. The evaluator instead reports hybrid as unavailable or failed, with
no fabricated score. Missing expected article paths invalidate the fixture;
missed questions remain in the report. See the
[32-question measurement and limitations](storage-options.md#measured-retrieval).

To reuse an already downloaded cache in another installation, copy the whole
model cache with its `refs`, `snapshots` and `blobs` layout and preserve relative
symlinks. Copying only `model.onnx` or only the snapshot directory is insufficient.
The measured cache occupies about 465 MB on disk.

After moving the cache, explicitly reindex against that installation's current
canonical articles, then evaluate offline. Rebuild the semantic index rather
than raw-copying a SQLite file with possible WAL state. Restart that
installation's MCP process after code or root changes; do not stop unrelated
agent sessions.

## Migration and cutover

The migration gate prevents new-code legacy writers from completing after
canonical publication. Spooling preserves hook deliveries while that gate is
held. Processes already running old code still require an initial quiescence
check; the copied-corpus evaluation alone does not prove live cutover or recovery.

For a legacy installation:

1. Identify the exact repository and deploy the new gated entry points.
   Let old-code worker and maintenance processes finish before migration;
   do not stop unrelated interactive agents. New-code hooks can spool during
   the migration. For an older installation without this gate, pause captures.
2. Run `uv run python scripts/memory_migrate.py` in that repository.
   For rehearsal, use `--root /isolated/root --source-root /legacy/root`;
   this reads the legacy corpus and writes only the isolated destination.
3. Retain the tar under `reports/migration-backups/` and the original files.
   Migration stages the database, checks integrity and article hashes, and
   publishes only after validation. It preserves UTF-8 article/source bytes,
   archived source aliases, legacy state and recoverable contexts.
4. Inspect health, counts, source provenance and queued jobs. Repeated migration
   recognizes an initialized store; an unknown existing database stops the
   operation instead of being replaced.
5. Reuse the model cache if available, explicitly reindex, and run retrieval
   evaluation. Confirm direct article and source reads work without relying on
   exported Markdown.
6. Check the configured runtime, drain recoverable jobs, compile pending source
   batches, and inspect every failure or quarantine. A completed migration
   alone does not prove pipeline recovery.
7. Export and review drift, then run the integrated acceptance gate. Resume
   any processors you explicitly paused after checking its results.

Migration does not delete the legacy corpus, nested Git history, temporary
contexts or permanent-recovery directories.
Legacy reservations were not proof of durable processing. Capture reuses
retained recovery ranges only when their complete sanitized text matches the
transcript. Unconfirmed historical ranges are conservatively recaptured and
labelled `legacy_unverified`; this can introduce one-time duplicate summaries
but does not silently discard sessions based on an unproven checkpoint.

## Backup and restore

Use `MemoryStore.backup(destination)`, which calls SQLite's online backup API.
It builds the complete snapshot privately and publishes the destination
exclusively. An existing file or symlink, including one created during the
backup, is never overwritten. An interrupted backup does not publish a partial
destination.
Choose a new destination; the method refuses to overwrite an existing file.
The backup is a standalone rollback-journal snapshot, safe to move and open
read-only without WAL companions. Migration enables WAL after publishing the
snapshot at its final path; restore tooling must do the same before restarting
writers, using `MemoryStore(root).initialize()` on the validated installation.
For example, from the repository root:

```bash
PYTHONPATH=scripts uv run python -c 'from pathlib import Path; from memory_store import MemoryStore; MemoryStore(Path.cwd()).backup(Path("reports/backups/pre-maintenance.sqlite"))'
```

Retain the resulting database, migration tar and any export-conflict history.
The model cache is reusable; the semantic index is rebuildable.
Do not copy only `memory.sqlite` while a WAL database is active: committed data
can still reside in its WAL. SQLite explains these constraints in its
[WAL documentation](https://www.sqlite.org/wal.html).

Restore deliberately:

1. Stop this installation's hooks, workers, MCP process and maintenance
   scheduler. Prevent new captures during the restore window.
2. Preserve the current database with the backup API if it is readable.
   If it is damaged, preserve the stopped database and its WAL/SHM companions
   together for investigation; do not treat that raw set as a verified backup.
3. Validate the selected backup in an isolated root with integrity and content
   checks. Confirm its timestamp and understand which newer jobs or sources
   require recovery from the preserved current state.
4. Move the old database and companions to a uniquely named recovery location
   before installing the validated snapshot. Never attach stale WAL files to
   the restored database, and never erase the recovery location as cleanup.
5. Run health, recover newer work deliberately, regenerate exports and rebuild
   retrieval indexes. Verify provenance and checkpoint consistency before
   restarting the installation.

A Git checkout of exported articles is not a database restore. Article
revisions support inspection of canonical history; use validated changes for
targeted recovery instead of arbitrary SQL or filesystem replacement.
