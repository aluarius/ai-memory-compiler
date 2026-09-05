# SQLite Agent Memory

The user approved the review's reliability, retrieval, and storage improvements and
confirmed that agents are the primary consumers. SQLite becomes the authority;
Markdown is a generated, human-readable interchange format.

## Data ownership

- `scripts/memory.sqlite` stores articles, revisions, source logs, captured session
  contexts, durable jobs, ingestion checkpoints, usage, and build events.
- Article bodies retain Markdown syntax and YAML frontmatter inside TEXT columns.
  This preserves readable prompts and exports without making files authoritative.
- Article paths remain stable external identifiers (`concepts/x`, `connections/x`,
  `qa/x`); internal IDs and revisions survive content changes.
- Sources remain append-only and preserve original text, session IDs, message
  ranges, project directories, and provider metadata. Migration never deletes the
  old source files, recovery contexts, JSON state, or nested knowledge Git history.
- SQLite uses foreign keys, WAL, explicit short write transactions, and schema
  versioning. LLM calls never run inside a database write transaction.

## Compiler and writers

Models return a validated JSON change set with exact text edits for existing
articles, complete bodies for new articles, and concise changed summaries.
Complete replacements remain compatible. Python applies edits only to the
original snapshot, rejecting missing or ambiguous matches; omitted metadata is
preserved. Models can read exported knowledge but cannot modify the live corpus.
Python validates paths, frontmatter, sources, and references, then commits changed
articles, revisions, build events, and ingestion checkpoints in one transaction.
Optimistic generation checks reject work based on an outdated article snapshot.
Malformed or incomplete output never advances ingestion. There is no Git rollback
of live user files. Exports happen after commit and can be retried independently.

The daily-log extraction path enqueues sanitized contexts durably before spawning
a worker or advancing a transcript checkpoint. Jobs have idempotency keys, leases,
attempts, next-attempt timestamps, and retained errors. Completing a flush and
appending its source entry is one transaction. Legacy failed contexts are imported
idempotently. Runtime incompatibility is visible and does not erase queued work.

## Retrieval

MCP reads articles and sources from SQLite. Full-text results retain snippets,
source references, and project filters. Optional local multilingual embeddings
combine semantic and lexical rankings; the base installation remains usable
without downloaded models. Embeddings are disposable and keyed by content hash
and model identity. Query execution never downloads a model unexpectedly.

SessionStart accepts the hook's cwd, selects matching project knowledge plus shared
rules, reserves space for hubs, and uses a byte-aware context budget. Archives are
searchable. Real-question evaluation compares lexical and hybrid Hit@5 and latency.

## Operations

Runtime calls are bounded and reap child processes. Background model selection is
explicit. Health distinguishes normal pending work for today from failed jobs,
expired leases, corrupt storage, stale exports, and a broken runtime. Maintenance
surfaces operational failures and exits nonzero when work failed. Structural graph
checks parse each article once rather than rescanning the corpus for each node.

## Migration and rollout

1. Back up the complete legacy corpus, state, recovery contexts, and nested Git.
2. Import and validate a disposable copy; compare all article/source bytes and counts.
3. Test round-trip exports, duplicates, invalid change sets, crashes, and concurrency.
4. Briefly serialize the actual cutover with existing writer locks; catch up newly
   written legacy sources and contexts before marking the database authoritative.
5. Run live health, retrieval evaluation, bounded provider smoke, and drain queued
   work with the repaired runtime. Retain backups and provide explicit restoration
   instructions. Do not push repository changes without a separate request.
