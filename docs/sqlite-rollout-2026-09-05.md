# SQLite rollout: 2026-09-05 to 2026-09-06

## Storage and preservation

The working installation uses `scripts/memory.sqlite` as its canonical store.
Markdown files remain reproducible exports; Obsidian is an optional reader.
The [storage comparison](storage-options.md) records the alternatives and
the reasons for selecting SQLite for local agent consumers.

Live migration imported 569 articles, 146 sources (31 active and 115 archived),
and 49 preserved extraction contexts. Subsequent hook deliveries enter the
same durable queue. Existing Markdown, recovery files and nested knowledge Git
history remain preserved.

Before recovering the queue, an independent comparison against the migration
tar confirmed all 569 article bodies match byte-for-byte. All 146 original
source bodies remain exact prefixes of their canonical sources; recovery
appends new entries without replacing earlier content. SQLite integrity and
foreign-key checks returned no errors.

Local recovery artifacts:

- `reports/migration-backups/2026-09-05T21-34-16+05-00-legacy.tar.gz`
- `reports/migration-backups/pre-recovery-20260905.sqlite`
- `reports/migration-backups/post-recovery-pre-compile-20260905.sqlite`
- `reports/migration-backups/final-acceptance-20260906.sqlite`
- `reports/migration-backups/runtime-config-before-20260905.json`

These contain personal data and are deliberately excluded from Git.
Use the [backup and restore procedure](operations.md#backup-and-restore), not
a checkout of exported Markdown, for database recovery.

## Runtime and portability

The project pins the native Codex CLI 0.153.4 and `gpt-6-astra` with medium
reasoning for background extraction and compilation. Isolated configuration
excludes interactive MCP/plugin settings. Global agent configuration, existing
interactive processes and scheduler definitions were not changed.

Bounded ordinary and isolated CLI smoke calls passed. Copied-corpus acceptance
completed a real extraction and a real compile batch, which atomically changed
three articles and advanced the processed-byte checkpoint. Those rehearsal
model changes were not copied into the live store.

Live acceptance exposed a portability difference: Python 3.12.2 / SQLite
3.51.0 could not initially open a relocated WAL-mode backup read-only without
sidecars. Python 3.13.13 / SQLite 3.50.4 could. Backups now use standalone
rollback-journal snapshots, and migration enables WAL at the final location.
The fix preserves the source database and has a relocated read-only regression
test. Independent review found no remaining High/Medium issue in that change.

After the fix, the complete suite passed on both interpreter combinations:
335 tests on Python 3.12.2 and 335 tests on Python 3.13.13. These are macOS
results, not Windows process-cleanup acceptance.

## Compile response-size follow-up

The first live batch committed ten article changes from 63,914 source bytes.
Eight updates repeated 84,769 UTF-8 bytes of existing complete article bodies;
the other two changes created articles. This exposed unnecessary response
volume for small changes in long articles.

The model contract now supports sequential exact replacements for existing
articles. Python requires a unique match, including overlapping occurrences,
and materializes a complete body against the original snapshot. Existing
schema, graph, revision and generation checks still apply. Omitted metadata is
preserved; summary-only maintenance does not need to repeat article bodies.
New articles still use complete bodies, and full replacements remain compatible.

Eighteen additional regressions cover successful materialization, rejected
matches and targets, rollback, stale generations, metadata and consolidation.
Independent review closed the overlapping-match and invalid-path findings.
The suite after this optimization passed all 353 tests on both interpreter combinations.

Live resumed acceptance used the new format successfully:

| Source bytes in batch | Model duration | Response bytes | Exact-edit articles | Complete-body articles |
| --- | ---: | ---: | ---: | ---: |
| 63,729 | 267.92 s | 30,364 | 8 | 0 |
| 7,120 | 90.29 s | 8,223 | 3 | 0 |

Both batches committed and advanced the checkpoint through byte 198,702.
These observations confirm use of the smaller response format, not a controlled
speed comparison: the earlier full-body batches processed different content.
The `live_compile_acceptance` runtime event retains the measured call results.
Codex does not report dollar cost through this wrapper; its zero cost field is
not evidence that model processing is free.

## Initial live retrieval acceptance

The frozen 32-question Russian fixture ran against the live 569-article store.
No expected paths were missing, and no fixture or ranking parameter changed
after the copied-corpus measurement.

| Metric | BM25 | Hybrid |
| --- | ---: | ---: |
| Hit@5 | 23/32 (71.9%) | 29/32 (90.6%) |
| Median latency | 1.959 ms | 102.169 ms |
| p95 latency | 3.748 ms | 115.378 ms |
| First query latency | 63.380 ms | 1,194.481 ms |

Offline reindexing reused the model cache. The evaluator and a fresh owned MCP
process made zero network connection attempts. MCP exposed all five tools,
returned a hybrid result for a Russian question, and read an archived source
in full (146,464 UTF-8 bytes), matching the canonical hash. The process exited
successfully; unrelated MCP processes were not stopped.

A direct live SessionStart invocation for the BK2 working directory returned
project articles and canonical MCP guidance in 9,493 UTF-8 bytes, below its
9,500-byte limit. The observed single invocation took 67.61 ms and exited 0;
this is a smoke measurement, not a latency distribution.

Canonical generation and article/source hashes stayed unchanged during this
acceptance. The complete local artifact is
`reports/retrieval-ru-32-live-2026-09-05.json`.

This initial result is historical: compilation subsequently changed the corpus
and exposed the retrieval regression below. The fixture remains unchanged.
Existing long-running MCP processes need a reconnect or new session to load
the new code and tools.

## Retrieval regression and correction

Some live model updates changed English articles into Russian. The updated
corpus contains 573 articles. On generation 7, the unchanged fixture scored
7/32 for BM25 and 19/32 for the original hybrid. Rare Russian function words
dominated lexical matches in the mostly English corpus.

A generic Russian/English function-word filter restored BM25 to 21/32 and the
original hybrid to 24/32 on that generation. Single-word queries and explicit
code keywords remain searchable. Tests cover uppercase operators inside both
Russian and English questions, as well as quoted lowercase expressions.

Separate diagnosis measured semantic-only at 28/32 on generation 7. The old
hybrid lost six semantic hits and gained two lexical hits: summed reciprocal
rank fusion rewarded weak agreement enough to displace three semantic leaders.
Hybrid now orders by best provider rank and uses reciprocal-rank agreement
only to break rank ties. Two synthetic tests reproduce the displacement for
each provider and pass after the fix; no project-specific weights were added.

The fixture is now explicitly a diagnostic regression set, not held-out proof.
No question, expected path or model variant was changed to improve the score.
All intermediate reports remain preserved, including the failed acceptance:

- `reports/retrieval-ru-32-final-2026-09-05.json` records the 7/32 and 19/32 regression.
- `reports/retrieval-ru-32-tokenizer-fixed-2026-09-05.json` records the first correction.
- `reports/retrieval-ru-32-rank-first-2026-09-06.json` records an intermediate run
  with explicit code-change caveats; it is not the final acceptance artifact.

## Final retrieval acceptance

The final offline run used generation 10, all 573 articles and the unchanged
32-question fixture. Article and source hashes, generation and all six checked
retrieval-code hashes stayed unchanged during the measurement. No expected
path was missing and no network connection was attempted.

| Metric | BM25 | Hybrid | Semantic-only diagnostic |
| --- | ---: | ---: | ---: |
| Hit@5 | 21/32 (65.6%) | 25/32 (78.1%) | 26/32 (81.3%) |
| Median latency | 4.465 ms | 107.011 ms | 97.683 ms |
| p95 latency | 7.226 ms | 123.626 ms | 107.761 ms |
| First query latency | 70.678 ms | 1,422.362 ms | 809.777 ms |

The semantic-only diagnostic uses the same snapshot and model, not a model
sweep. Hybrid gains the Vite question but loses the OpenLayers trapezoid and
AI-prose questions relative to semantic-only, for one fewer hit overall. The
new fusion rule protects provider leaders; it does not establish that hybrid
always beats either provider. Seven hybrid misses remain and require a larger
independent evaluation set before further relevance tuning.

A fresh owned MCP process exposed all five tools, returned the expected hybrid
answer candidate for a Russian question and read the archived source in full
(146,464 UTF-8 bytes), matching the canonical hash. It exited 0. The final
artifact, `reports/retrieval-ru-32-accepted-2026-09-06.json`, records every query,
returned path, miss, code/corpus fingerprint and the MCP acceptance result.

These figures describe this local corpus, not answer correctness or statistical
superiority. Latency includes local host contention and process initialization;
do not treat differences from previous runs as a controlled performance study.
Reconnecting existing MCP sessions is necessary to load the final code.

## Recovery and final acceptance

All 49 migrated extraction jobs completed without failures or quarantine.
New hook deliveries from active sessions continue to enter the normal queue;
they are tracked separately from the migrated backlog. A further portable
snapshot preserves the recovered source entries before live compilation.

Two full-body batches committed through byte 127,853 before switching to the
new contract. Only the controller-owned read-only model subprocess was stopped
for that switch; no committed batch was reverted. A fresh invocation resumes
from that checkpoint and measures its actual response formats and sizes.

The resumed invocation completed at 23:06:22 +05 on September 5. Its validated
checkpoint covers all migrated recovery entries. The automatic compiler then
processed newer hook deliveries and finished at 00:14:53 +05 on September 6.
Its final checkpoint, 359,361 bytes, matches the complete source hash. No
uncompiled or stale source remains at acceptance; all 100 jobs are done.

Guarded mechanical maintenance added the final four backlinks. The store now
contains 573 articles and 146 sources at generation 10. Export is synchronized,
offline semantic indexing covers all 573 articles, and database integrity and
foreign-key checks return no errors. The final backup uses standalone journal
header bytes `1,1`; it does not depend on live WAL sidecars.

`health.py --json --strict` exits 0 with status `ok`: no errors, warnings,
pending/failed/quarantined jobs, expired leases, capture spools or export drift.
One weak-connectivity suggestion remains. Semantic contradiction checking was
not run as part of this structural acceptance; that requires a separate model
judgment and is not implied by a clean structural result.

After the last code change, the complete suite passed **368 tests** on both
Python 3.12.2 (10.28 s) and Python 3.13.13 (7.24 s). Ruff's E4/E7/E9/F checks and
`git diff --check` passed. Independent review found no remaining High/Medium
issue in the retrieval changes.

Concurrent full-suite runs briefly failed the 0.5-second subprocess-startup
test before its child wrote a PID file. Focused and sequential full reruns
passed; the reported final results are those sequential runs. These checks do
not establish Windows subprocess behavior or future provider availability.

Local commits contain the canonical migration (`baf6266`), portable backup fix
(`dfe87f6`), exact-edit compiler contract (`10852ea`) and retrieval corrections
(`e7f0a09`). Nothing was pushed. The isolated rehearsal worktree and all recovery
artifacts remain preserved; there was no forced cleanup or destructive rollback.
