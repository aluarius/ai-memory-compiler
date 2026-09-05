# Storage options

## Decision

Use SQLite as the canonical application store, with optional Markdown exports
and a separate disposable retrieval index. This decision addresses capture
durability and transactional compilation first; embeddings address a different
problem: finding relevant knowledge.

The workload consists of several local hook/worker processes, frequent small
writes, append-only sources, article revisions and checkpoints that must commit
together. Model calls run outside database transactions.

## Alternatives reviewed

| Option | What it provides | Fit for this compiler |
| --- | --- | --- |
| SQLite application store | Local transactions, relational constraints, WAL and an application-owned schema | Selected: jobs, sources, revisions and checkpoints share one transaction boundary |
| Basic Memory | Markdown knowledge graph, MCP tools, indexing and configurable retrieval | Useful when editable Markdown remains the primary knowledge format; adopting it does not by itself supply this compiler's job/checkpoint contract |
| QMD | Local BM25, vector retrieval, query expansion and reranking over documents | Useful retrieval component, not a replacement for transactional capture and compilation |
| DuckDB native store | Analytical queries and concurrent threads inside an owning process | Adds coordination for this multi-process, small-write workload; better suited to downstream analysis |

SQLite explicitly supports the application-file use case. WAL allows readers
alongside a writer, but writes still serialize and all participating processes
must use the same host. Keep the database on local storage and use short
transactions plus SQLite backups. See the official
[application file format guidance](https://www.sqlite.org/appfileformat.html)
and [WAL constraints](https://www.sqlite.org/wal.html).

Basic Memory documents plain Markdown as its knowledge format and exposes
configuration for indexing, project routing and semantic retrieval. A database
backend or index does not make that product equivalent to this repository's
SQLite-primary compiler. See its
[knowledge format](https://docs.basicmemory.com/concepts/knowledge-format)
and [configuration reference](https://docs.basicmemory.com/reference/configuration).

QMD combines BM25, semantic search and local model reranking. Its retrieval
pipeline is broader than the small BM25-plus-embedding layer used here.
Treating QMD as a possible search component, rather than a transactional
compiler, is our architectural assessment of its documented scope.
See the [official repository](https://github.com/tobi/qmd).

DuckDB's embedded read-write mode has one owning process. Its current
documentation also describes multi-process writing through a client-server
protocol and other coordinated deployments; it is not correct to say DuckDB
can never support multiple writers. Those alternatives add infrastructure
this local queue does not need. See the official
[concurrency reference](https://duckdb.org/docs/stable/connect/concurrency)
([current version](https://duckdb.org/docs/current/connect/concurrency)).
The fit judgment concerns this workload, not a claim that DuckDB lacks transactions.

## Measured retrieval

On 2026-09-05, a copied canonical corpus contained 569 articles and 146 sources.
The [gold fixture](../tests/fixtures/retrieval-ru.json) contains 32 distinct Russian
questions across 24 domain labels, with reviewed expected article paths, source
references and evidence notes. Questions paraphrase genuine recorded incidents;
they are not presented as verbatim user transcripts.

| Metric | BM25 | BM25 + multilingual-e5-small |
| --- | ---: | ---: |
| Hit@5 | 23/32 (71.9%) | 29/32 (90.6%) |
| Median query latency | 1.892 ms | 98.168 ms |
| p95 latency | 3.841 ms | 133.046 ms |
| First query latency | 65.461 ms | 1,718.869 ms |

Hit@5 means at least one expected article appears in the first five results.
The median reflects predominantly warm queries in one process; the first query
includes initialization. This is not answer correctness, complete source
recall, or end-to-end agent latency.

The hybrid uses the official `intfloat/multilingual-e5-small` ONNX model,
384-dimensional mean-pooled normalized embeddings, and reciprocal rank fusion
with BM25. The cache uses model revision
`614241f622f53c4eeff9890bdc4f31cfecc418b3`.
It runs locally through optional FastEmbed, without query expansion or a reranker.

The expanded fixture is frozen before measurement. Its original ten cases,
including misses, remain unchanged; no question or ranking parameter is tuned
after seeing the expanded results. All expected article paths exist, and the
review confirms the referenced sources and article-source links.

Both modes run with network connections blocked: zero connection attempts
occur. Canonical generation and article/source hashes remain unchanged.
The local report is retained at
`reports/retrieval-ru-32-offline-2026-09-05.json` (reports are not versioned).
Fixture SHA-256:
`a2553945f65155f8c4cd3f4e25b246d3dc51d5fef860a3ab55b95d6ce83815e9`.

Hybrid still misses questions about Russian regex boundaries, stale async
responses during rapid product selection, and sequence drift after importing
explicit IDs. These cases stay in the fixture.

This small, manually reviewed, corpus-specific set is not an independent
held-out benchmark and does not establish broad statistical superiority.
It supports enabling optional hybrid retrieval while retaining explicit BM25,
visible fallback status and reproducible evaluation. Other tools in the
comparison are not benchmarked here.

## Operational consequences

Canonical storage remains useful when Markdown is absent or embeddings fail.
Normal MCP queries never download models. Explicit indexing can download and
rebuild the separate cache; evaluation treats unavailable hybrid retrieval and
missing expected paths as failures rather than scoring a hidden fallback.

Obsidian reads an optional export. External edits require conflict review and
explicit incorporation into canonical memory. Database backup and restore,
not Git checkout of Markdown, define recovery.

The code and live-cutover acceptance gate are being integrated. The copied
corpus measurement above does not claim that live migration, backlog recovery
or final export acceptance has completed. See [operations](operations.md).
