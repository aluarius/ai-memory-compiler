# Codex Transcript Notes

This project now supports direct import of the JSONL session format currently written by the local Codex CLI.

## Observed Local Storage

- Session index: `~/.codex/session_index.jsonl`
- History log: `~/.codex/history.jsonl`
- Full session transcripts: `~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl`

These paths were observed locally and are treated as implementation details, not a guaranteed public API.

## Hook Contract

The documented Codex `Stop` hook now passes the active transcript path on stdin.
The fields this project currently relies on are:

- `transcript_path`
- `session_id`
- `cwd`
- `model`
- `turn_id`
- `stop_hook_active`

That means the hook should import the specific transcript Codex names for the
current turn, and use directory scanning only as a compatibility fallback for
older CLI builds.

## Supported JSONL Shape

The importer reads the current Codex session shape conservatively:

- `session_meta.payload`
  - `id`
  - `cwd`
  - `model_provider`
  - optional `source`
- `turn_context.payload`
  - `model`
  - optional `cwd`
- `response_item.payload`
  - only `type == "message"`
  - only `role in {"user", "assistant"}`
  - text blocks from `input_text`, `output_text`, or `text`

Ignored on purpose:

- `developer` messages
- reasoning items
- tool/function call records
- other non-message event types

## Why This Matters

`scripts/import_session.py` no longer guesses a generic JSONL structure for Codex. It detects `codex_jsonl`, extracts normalized conversation context, and carries forward useful metadata such as `session_id`, `provider`, `model`, and `cwd`.
