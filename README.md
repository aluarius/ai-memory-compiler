# AI Memory Compiler

> Originally forked from [coleam00/claude-memory-compiler](https://github.com/coleam00/claude-memory-compiler), now an independent project with multi-agent support.

**Your AI conversations compile themselves into a searchable knowledge base.**

Works with **Claude Code** and **Codex**. Sessions are captured automatically via hooks, important knowledge is extracted into daily logs, then compiled into structured, cross-referenced articles. At the start of every session your agent gets the knowledge base index — so it "remembers" what it learned before.

No vector database, no embeddings, no RAG — just markdown and an index the LLM reads directly. At personal scale (up to ~500 articles) this [outperforms vector similarity](https://gist.github.com/karpathy/442a6bf555914893e9891c11519de94f).

Claude Agent SDK usage is covered by your Claude subscription (Max, Team, or Enterprise) — no extra API costs.

## Quick Start

```bash
git clone https://github.com/aluarius/ai-memory-compiler
cd ai-memory-compiler
uv sync
```

Then configure hooks for your agent(s).

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

- **SessionStart** — injects knowledge base index into every session
- **SessionEnd** — captures the conversation and extracts knowledge
- **PreCompact** — safety net before auto-compaction discards context

### Codex

Codex hooks are documented now, but they are still experimental and disabled by
default.

File 1: `~/.codex/config.toml`

Enable the feature flag:

```toml
[features]
codex_hooks = true
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
          "command": "cd /path/to/ai-memory-compiler && uv run python hooks/codex-stop.py",
          "timeout": 10
        }]
      }
    ]
  }
}
```

- **SessionStart** — same knowledge base injection as Claude Code
- **Stop** — turn-scoped auto-import using Codex's official `transcript_path`
  hook payload; rate-limited per session to avoid repetitive rolling summaries,
  and falls back to transcript scanning only for older builds
- Codex does not currently provide a true session-end hook equivalent to
  Claude Code's `SessionEnd`
- If you define the same hook in both `~/.codex/hooks.json` and
  `<repo>/.codex/hooks.json`, Codex runs both

### MCP Server (optional)

Gives Claude Code tools to search the knowledge base mid-session. Add to `~/.claude/.mcp.json`:

```json
{
  "mcpServers": {
    "knowledge-base": {
      "command": "uv",
      "args": ["run", "--directory", "/path/to/ai-memory-compiler", "python", "scripts/mcp_server.py"]
    }
  }
}
```

Tools: `search_knowledge`, `list_articles`, `read_article`, `search_daily_logs`.

## How It Works

```
Session ends -> hook captures transcript -> sanitize secrets -> flush.py extracts knowledge
  -> daily/YYYY-MM-DD.md -> compile.py -> knowledge/concepts/, connections/, qa/
    -> next session starts -> hook injects knowledge index -> agent "remembers"
```

- **flush.py** — decides what's worth saving (runs after every session, retries on failures)
- **compile.py** — compiles daily logs into structured wiki articles (auto-triggers after 10 PM)
- **Sensitive data** (API keys, tokens, passwords) is redacted before anything is saved
- **Old logs** are archived after 30 days

## Key Commands

```bash
uv run python scripts/compile.py                     # compile new daily logs
uv run python scripts/compile.py --dry-run            # preview what would compile
uv run python scripts/query.py "question"             # ask the knowledge base
uv run python scripts/lint.py --structural-only       # health checks (free)
uv run python scripts/import_session.py transcript.jsonl --agent codex  # manual import
```

## What's Different from Upstream

Added on top of the original [coleam00/claude-memory-compiler](https://github.com/coleam00/claude-memory-compiler):

- **Codex support** — automatic capture via documented Codex hooks, with
  turn-scoped `Stop` behavior instead of Claude Code's `SessionEnd`
- **Official Codex hook support** — uses documented `hooks.json` payloads for
  `SessionStart` and `Stop`
- **MCP server** — search and read knowledge base articles from any session
- **Secret redaction** — API keys, tokens, passwords masked before saving
- **Global hooks** — capture sessions from all projects, not just this one
- **Reliability** — retry logic, file locking, proper process detachment, temp cleanup

## Technical Reference

See **[AGENTS.md](AGENTS.md)** for the complete technical reference: article formats, hook architecture, script internals, and customization options.
