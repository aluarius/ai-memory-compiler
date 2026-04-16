# LLM Personal Knowledge Base

> Fork of [coleam00/claude-memory-compiler](https://github.com/coleam00/claude-memory-compiler) with additional features (see [What's different](#whats-different-from-upstream)).

**Your AI conversations compile themselves into a searchable knowledge base.**

Adapted from [Karpathy's LLM Knowledge Base](https://gist.github.com/karpathy/442a6bf555914893e9891c11519de94f) architecture, but instead of clipping web articles, the raw data is your own AI conversations. Claude Code can feed it automatically via hooks, and other agents such as Codex can feed the same pipeline via transcript import. Sessions are normalized into daily logs, then compiled into structured, cross-referenced knowledge articles organized by concept. Retrieval uses a simple index file instead of RAG - no vector database, no embeddings, just markdown.

Anthropic has clarified that personal use of the Claude Agent SDK is covered under your existing Claude subscription (Max, Team, or Enterprise) - no separate API credits needed.

## Quick Start

Tell your AI coding agent:

> "Clone https://github.com/aluarius/claude-memory-compiler into this project. Set up the Claude Code hooks so my conversations automatically get captured into daily logs, compiled into a knowledge base, and injected back into future sessions. Read the AGENTS.md for the full technical reference on how everything works."

The agent will:
1. Clone the repo and run `uv sync` to install dependencies
2. Add hooks to your `~/.claude/settings.json` (global — captures all sessions)
3. Register the MCP server in `~/.claude/.mcp.json` for knowledge base access
4. The hooks activate automatically next time you open Claude Code

## How It Works

```
Conversation -> SessionEnd/PreCompact hooks -> sanitize -> flush.py extracts knowledge
    -> daily/YYYY-MM-DD.md -> compile.py -> knowledge/concepts/, connections/, qa/
        -> lint (post-compile health check) -> archive old logs
        -> SessionStart hook injects index into next session -> cycle repeats
```

- **Hooks** capture conversations automatically (session end + pre-compaction safety net)
- **External session import** lets non-Claude agents feed the same memory pipeline (`scripts/import_session.py`)
- **Sanitizer** redacts API keys, tokens, passwords before persisting to daily logs
- **flush.py** decides what is worth saving via the configured processing runtime (`Claude` by default, optionally `Codex`), with retry logic for transient failures. Operational failures are written to `reports/runtime-events.md` instead of polluting the daily corpus. After 10 PM triggers end-of-day compilation automatically
- **compile.py** turns daily logs into organized concept articles with cross-references, runs post-compile health checks, and archives old logs (30+ days)
- **MCP server** exposes `search_knowledge`, `list_articles`, `read_article`, `search_daily_logs` tools — accessible from any Claude Code session
- **query.py** answers questions using index-guided retrieval (no RAG needed at personal scale)
- **lint.py** runs 7 health checks (broken links, orphans, contradictions, staleness)
- **Per-task runtime switch** lets `flush`, `compile`, `query`, and contradiction lint run through `Claude` or `Codex` via `scripts/runtime-config.json`

## Key Commands

```bash
uv run python scripts/compile.py                    # compile new daily logs
uv run python scripts/compile.py --dry-run           # see what would be compiled
uv run python scripts/query.py "question"            # ask the knowledge base
uv run python scripts/query.py "question" --file-back # ask + save answer back
uv run python scripts/lint.py                        # run health checks
uv run python scripts/lint.py --structural-only      # free structural checks only
uv run python scripts/import_session.py /path/to/transcript.jsonl --agent codex --provider openai
```

Runtime selection lives in `scripts/runtime-config.json`. Default is all `claude`.
Example:

```json
{
  "flush_runtime": "claude",
  "compile_runtime": "claude",
  "query_runtime": "codex",
  "lint_runtime": "claude",
  "codex_model": "gpt-5.4"
}
```

## MCP Server

The knowledge base is available as an MCP server for Claude Code. Register in `~/.claude/.mcp.json`:

```json
{
  "mcpServers": {
    "knowledge-base": {
      "command": "uv",
      "args": ["run", "--directory", "/path/to/claude-memory-compiler", "python", "scripts/mcp_server.py"]
    }
  }
}
```

Available tools:
- `search_knowledge(query)` — search articles by keyword
- `list_articles()` — list all articles with summaries
- `read_article(path)` — read a specific article
- `search_daily_logs(query, last_n_days)` — search recent session logs

`read_article()` now validates paths and will refuse attempts to escape `knowledge/`.

## Mixed Agent Workflow

The knowledge base can combine multiple coding agents as long as they feed the
same normalized daily-log pipeline.

- **Claude Code**: automatic capture via hooks
- **Codex or other agents**: import a saved transcript with `scripts/import_session.py`

For a thin interactive wrapper around Codex that auto-imports the finished
session after exit:

```bash
uv run python scripts/codex_session.py -- -m gpt-5.4
uv run python scripts/codex_session.py -- resume --last
```

Imported sessions are tagged in the daily log with source metadata
(`agent=... | provider=... | session=...`) so mixed-agent history stays auditable.

For the pragmatic rollout plan for Codex support without overengineering, see
[docs/codex-parity-architecture.md](docs/codex-parity-architecture.md). For the
currently supported Codex transcript shape, see
[docs/codex-transcripts.md](docs/codex-transcripts.md).

## Security

Sensitive data (API keys, tokens, passwords, private keys, connection strings) is automatically redacted before being written to daily logs. See `hooks/sanitize.py` for the full list of patterns.

## Why No RAG?

Karpathy's insight: at personal scale (50-500 articles), the LLM reading a structured `index.md` outperforms vector similarity. The LLM understands what you're really asking; cosine similarity just finds similar words. RAG becomes necessary at ~2,000+ articles when the index exceeds the context window.

## What's Different from Upstream

This fork adds the following on top of [coleam00/claude-memory-compiler](https://github.com/coleam00/claude-memory-compiler):

- **MCP server** — knowledge base accessible as tools from any Claude Code session (`search_knowledge`, `list_articles`, `read_article`, `search_daily_logs`)
- **Sensitive data redaction** — API keys, tokens, passwords, private keys are automatically masked before writing to daily logs (`hooks/sanitize.py`)
- **Retry with stderr capture** — flush.py retries on transient CLI failures and captures real stderr for diagnostics instead of the generic "Check stderr output for details"
- **Post-compile health checks** — structural lint runs automatically after every compilation
- **Log retention** — compiled daily logs older than 30 days are archived to `daily/archive/`
- **Temp file cleanup** — orphaned context files from crashed flushes are cleaned up automatically
- **Global hooks** — hooks configured in `~/.claude/settings.json` to capture sessions from all projects, not just this one
- **Shared multi-agent ingestion** — Claude hooks plus transcript import for Codex/other agents into one KB
- **File locking** — concurrent flush/compile processes safely share state files and daily logs (`scripts/locking.py`)
- **Shared session parsing** — hooks deduplicated into `scripts/session_utils.py` with normalized `SessionMetadata`
- **Proper process detachment** — background flush/compile processes detach from hook session group on all platforms
- **Path traversal prevention** — MCP `read_article()` validates paths and refuses escapes from `knowledge/`
- **Test suite** — 18 tests covering transcript parsing, sanitization, config, locking, and utilities

## Technical Reference

See **[AGENTS.md](AGENTS.md)** for the complete technical reference: article formats, hook architecture, script internals, cross-platform details, costs, and customization options.
