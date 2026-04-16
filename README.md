# LLM Personal Knowledge Base

> Fork of [coleam00/claude-memory-compiler](https://github.com/coleam00/claude-memory-compiler) with additional features (see [What's different](#whats-different-from-upstream)).

**Your AI conversations compile themselves into a searchable knowledge base.**

Works with **Claude Code** and **Codex**. Sessions are captured automatically via hooks, important knowledge is extracted into daily logs, then compiled into structured, cross-referenced articles. At the start of every session your agent gets the knowledge base index — so it "remembers" what it learned before.

No vector database, no embeddings, no RAG — just markdown and an index the LLM reads directly. At personal scale (up to ~500 articles) this [outperforms vector similarity](https://gist.github.com/karpathy/442a6bf555914893e9891c11519de94f).

Claude Agent SDK usage is covered by your Claude subscription (Max, Team, or Enterprise) — no extra API costs.

## Quick Start

```bash
git clone https://github.com/aluarius/claude-memory-compiler
cd claude-memory-compiler
uv sync
```

Then configure hooks for your agent(s).

### Claude Code

Add to `~/.claude/settings.json` inside the `"hooks"` object:

```json
"SessionStart": [
  {
    "matcher": "",
    "hooks": [{
      "type": "command",
      "command": "cd /path/to/claude-memory-compiler && uv run python hooks/session-start.py",
      "timeout": 15
    }]
  }
],
"PreCompact": [
  {
    "matcher": "",
    "hooks": [{
      "type": "command",
      "command": "cd /path/to/claude-memory-compiler && uv run python hooks/pre-compact.py",
      "timeout": 10
    }]
  }
],
"SessionEnd": [
  {
    "matcher": "",
    "hooks": [{
      "type": "command",
      "command": "cd /path/to/claude-memory-compiler && uv run python hooks/session-end.py",
      "timeout": 10
    }]
  }
]
```

- **SessionStart** — injects knowledge base index into every session
- **SessionEnd** — captures the conversation and extracts knowledge
- **PreCompact** — safety net before auto-compaction discards context

### Codex

Add to `~/.codex/hooks.json` inside the `"hooks"` object:

```json
"SessionStart": [
  {
    "hooks": [{
      "type": "command",
      "command": "cd /path/to/claude-memory-compiler && uv run python hooks/session-start.py",
      "timeout": 15
    }]
  }
],
"Stop": [
  {
    "hooks": [{
      "type": "command",
      "command": "cd /path/to/claude-memory-compiler && uv run python hooks/codex-stop.py",
      "timeout": 10
    }]
  }
]
```

- **SessionStart** — same knowledge base injection as Claude Code
- **Stop** — auto-imports the latest transcript from `~/.codex/sessions/`

### MCP Server (optional)

Gives Claude Code tools to search the knowledge base mid-session. Add to `~/.claude/.mcp.json`:

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

This fork adds on top of [coleam00/claude-memory-compiler](https://github.com/coleam00/claude-memory-compiler):

- **Codex support** — automatic session capture via hooks, same as Claude Code
- **MCP server** — search and read knowledge base articles from any session
- **Secret redaction** — API keys, tokens, passwords masked before saving
- **Global hooks** — capture sessions from all projects, not just this one
- **Reliability** — retry logic, file locking, proper process detachment, temp cleanup

## Technical Reference

See **[AGENTS.md](AGENTS.md)** for the complete technical reference: article formats, hook architecture, script internals, and customization options.
