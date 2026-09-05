# AI Memory Compiler

Originally forked from [coleam00/claude-memory-compiler](https://github.com/coleam00/claude-memory-compiler).

Compile Claude Code and Codex conversations into durable, searchable memory.
Hooks queue sanitized context, a model extracts useful daily notes, and a
validated compiler turns those notes into cross-referenced articles.

SQLite owns sources, articles, revisions, checkpoints and retry jobs.
Markdown remains available as an optional export for reading or Obsidian.
Agents retrieve canonical content through MCP, using BM25 or optional local
multilingual hybrid search.

## Quick start

Requires Python 3.12+ and `uv`.

```bash
git clone https://github.com/aluarius/ai-memory-compiler
cd ai-memory-compiler
uv sync
uv run python scripts/memory_migrate.py
uv run python scripts/health.py --json
```

Migration initializes an empty store or imports the existing legacy corpus.
For an existing installation, stop its capture and maintenance processors
first and follow the [migration checklist](docs/operations.md#migration-and-cutover).
Migration retains the original files and a legacy tar backup.

Configure your model runtime and hooks next. Extraction and compilation use
the selected Claude or Codex runtime and its credentials; costs and account
limits depend on that runtime. Retrieval does not call a hosted model.

### Claude Code

File: `~/.claude/settings.json`

Add these entries inside the top-level `"hooks"` object:

```json
"SessionStart": [
  {
    "matcher": "",
    "hooks": [{
      "type": "command",
      "command": "cd /path/to/ai-memory-compiler && uv run python hooks/session-start.py",
      "timeout": 15
    }]
  }
],
"PreCompact": [
  {
    "matcher": "",
    "hooks": [{
      "type": "command",
      "command": "cd /path/to/ai-memory-compiler && uv run python hooks/pre-compact.py",
      "timeout": 10
    }]
  }
],
"SessionEnd": [
  {
    "matcher": "",
    "hooks": [{
      "type": "command",
      "command": "cd /path/to/ai-memory-compiler && uv run python hooks/session-end.py",
      "timeout": 10
    }]
  }
]
```

- **SessionStart** injects a project-aware, byte-bounded catalog slice.
- **SessionEnd** durably queues sanitized context for background extraction.
- **PreCompact** captures context before compaction discards it.

> Troubleshooting: `SessionStart hook (failed) — exited with code 127` means
> `uv` is not on PATH in the environment Claude Code was launched from (e.g.
> the desktop app doesn't source your shell profile). Use an absolute path in
> the hook command: `/opt/homebrew/bin/uv run python hooks/session-start.py`
> (`which uv` shows yours).
>
> `Stop hook (failed) — No such file or directory (os error 2)` from Codex
> usually means the session's working directory no longer exists (the project
> folder was renamed or deleted mid-session) — spawning any child process
> then fails. The hook config is fine; restart the session in the new
> location. Turns after the last successful flush stay in the session's
> rollout under `~/.codex/sessions/` and can be imported manually with
> `scripts/import_session.py` if they mattered.

### Codex

For a Codex build that supports the hook feature flag, enable hooks as follows.

File 1: `~/.codex/config.toml`

Enable the feature flag:

```toml
[features]
hooks = true
```

File 2: `~/.codex/hooks.json`

Add the hook configuration there. You can also use `<repo>/.codex/hooks.json`
for repo-local hooks, but this project's recommended setup is the global file:

```json
{
  "hooks": {
    "SessionStart": [
      {
        "matcher": "startup|resume",
        "hooks": [{
          "type": "command",
          "command": "cd /path/to/ai-memory-compiler && uv run python hooks/session-start.py",
          "timeout": 15
        }]
      }
    ],
    "Stop": [
      {
        "hooks": [{
          "type": "command",
          "command": "cd /path/to/ai-memory-compiler && MEMORY_CODEX_BIN=/absolute/path/to/codex uv run python hooks/codex-stop.py",
          "timeout": 10
        }]
      }
    ]
  }
}
```

- **SessionStart** uses the same canonical context as Claude Code.
- **Stop** captures unseen turns from `transcript_path`, with a legacy
  transcript-scan fallback. Queue insertion and capture checkpoint advancement
  share a transaction; a failed worker spawn does not lose the queued range.
- Use the real Codex executable, not a GUI wrapper. A project `codex_bin` in
  `scripts/runtime-config.json` overrides the `MEMORY_CODEX_BIN` fallback shown
  above. See [runtime configuration](docs/operations.md#runtime-configuration).
- Stop is turn-scoped; this integration does not treat it as session end.
- Configure each hook in one place. Global and repository-local definitions
  can both run.

### MCP retrieval

Register the stdio server with Claude Code:

```bash
claude mcp add --scope user knowledge-base -- uv run --directory /path/to/ai-memory-compiler python scripts/mcp_server.py
```

Other MCP clients use command `uv` with these arguments:

```json
["run", "--directory", "/path/to/ai-memory-compiler", "python", "scripts/mcp_server.py"]
```

The server exposes five tools:

| Tool | Purpose |
| --- | --- |
| `search_knowledge(query, project=None, mode="hybrid")` | Find articles; `mode="bm25"` selects lexical search |
| `list_articles(project=None)` | List all articles or one project's articles |
| `read_article(path)` | Read the complete canonical article |
| `search_daily_logs(query, last_n_days=7, include_archive=True)` | Search active and archived sources; use `0` for all dates |
| `read_source(path)` | Read exact source provenance, including archive aliases |

The hook uses the session's `cwd` to select project and shared context within
9,500 UTF-8 bytes. MCP reads SQLite even if exported Markdown is absent.
An unavailable semantic index gives an explicit BM25 fallback, not a model
download.

### Optional local semantic search

```bash
uv sync --extra semantic
uv run --extra semantic python scripts/semantic_search.py index
uv run --extra semantic python scripts/evaluate_retrieval.py
```

The explicit indexing command can download the official
`intfloat/multilingual-e5-small` model into `scripts/.models/`.
Normal queries load only cached files. The separate
`scripts/semantic-index.sqlite` is disposable and keyed by model and article
content. Reindex after article changes; rerunning skips unchanged embeddings.

On a copied 569-article corpus, 32 reviewed Russian questions improved Hit@5
from 71.9% with BM25 to 90.6% with hybrid search. Median latency increased
from 1.9 ms to 98 ms. This is a small, corpus-specific gold set, not a general
quality guarantee. See [measurement details](docs/storage-options.md#measured-retrieval).

## Routine commands

```bash
uv run python scripts/health.py --json
uv run python scripts/flush.py --drain --limit 10
uv run python scripts/compile.py --dry-run
uv run python scripts/compile.py
uv run python scripts/lint.py --structural-only
uv run python scripts/memory_export.py
```

Queue processing and compilation can call the configured model. Health,
dry-run, structural lint, export and BM25 search do not.
A failed export leaves canonical commits intact; retry the export separately.

Manual transcript import remains available:

```bash
uv run python scripts/import_session.py transcript.jsonl --agent codex
```

For macOS unattended maintenance, review paths and environment in
[the launchd template](docs/launchd-maintenance.plist) before installing it.
The project `codex_bin` pin takes precedence over a launchd
`MEMORY_CODEX_BIN` value.

## Status and references

The SQLite implementation and cutover acceptance gate are being integrated.
The copied-corpus retrieval measurement is complete; it does not establish
that the live queue has drained or the live installation has switched over.

- [Agent and compiler contract](AGENTS.md)
- [Operations, backup and recovery](docs/operations.md)
- [Storage alternatives and retrieval evidence](docs/storage-options.md)
