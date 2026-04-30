# Codex Integration: Lean Plan

## Why This Document Exists

The previous Codex expansion plan was too heavy for the actual scale of this
project:

- one user
- one repository
- a tiny knowledge base
- working plain scripts

This document replaces that overbuilt plan with a practical one.

## Current Reality

What already exists:

- `Claude Code` sessions are captured automatically via hooks
- `Codex` sessions can be imported manually through [scripts/import_session.py](../scripts/import_session.py)
- `Codex` now has documented experimental hooks via `hooks.json`
- session metadata is already normalized enough for mixed-agent history in [scripts/session_utils.py](../scripts/session_utils.py)
- per-task processing runtime can now be selected in `scripts/runtime-config.json`
- Claude remains the default processing backend

What does **not** exist today:

- a second processing provider actually wired into the project

That means the project is currently at:

- `Claude = full runtime`
- `Codex = manual import + lightweight hook capture`

That is a useful state already. It does not need an enterprise architecture to be valid.

## Non-Goals For Now

Do not build these yet:

- `capture/`, `providers/`, `pipeline/`, `models/`, `orchestration/` trees
- base classes for hypothetical runtimes
- provider abstraction before the simple runtime switch stops being enough
- OpenAI processing backend for `flush` / `compile` / `query`

Each of those adds complexity before there is evidence the project needs it.

## Actual Goal

Support Codex in the simplest form that is worth maintaining:

1. Codex transcripts can be imported cleanly.
2. Mixed Claude/Codex sessions are distinguishable in `daily/`.
3. The rest of the system can stay mostly Claude-backed unless there is a concrete reason to change it.

## Recommended Architecture

Keep the existing flat scripts.

Add only small seams where they are already justified:

- `scripts/session_utils.py` for normalized session metadata and transcript parsing
- `scripts/import_session.py` for non-Claude sources
- `scripts/runtime-config.json` for choosing `claude` vs `codex` per task
- optional small helper functions for transcript parsing if Codex format differs from Claude JSONL

In other words:

- keep `flush.py`, `compile.py`, `query.py`, `lint.py` as top-level scripts
- keep Claude as the default processing backend
- treat Codex as an additional input source, not as a reason to redesign the app

## What Is Now Known

The repo no longer needs to guess the basic Codex integration surface:

1. Codex writes JSONL transcripts under `~/.codex/sessions/...`.
2. The `Stop` hook passes `transcript_path`, `session_id`, `cwd`, `model`, and `turn_id` on stdin.
3. Hooks are still experimental and require `[features] codex_hooks = true`.

The remaining work is hardening around those contracts, not inventing a new architecture.

## Cost Reality

This project currently has an important asymmetry:

- `claude_agent_sdk` is covered by Claude subscription usage
- OpenAI-backed processing would be real API spend

That matters.

Because of that:

- prefer leaving `flush`, `compile`, and `lint` on Claude until Codex shows a concrete benefit there
- use the runtime switch selectively instead of trying to move everything at once
- only expand beyond the current switch if there is a real benefit in quality, latency, or capability

Codex as a **source of transcripts** is cheap.
Codex/OpenAI as a **processing backend** is a different decision.

## Practical Rollout

### Step 0: Stay Where The Project Already Works

Today the supported mixed-agent workflow is:

```bash
uv run python scripts/import_session.py /path/to/codex-session.jsonl --agent codex --provider openai
```

This is the baseline. It is enough until research says otherwise.

The supported processing switch is:

- edit `scripts/runtime-config.json`
- choose `claude` or `codex` per task
- keep `claude` as the default unless there is a specific reason to switch

### Step 1: Keep The Integration Aligned With The Real Contract

Keep validating against real Codex releases:

- transcript format
- hook stdin payload
- any lifecycle changes that affect `Stop`

### Step 2: Harden Manual Import

If Codex transcript format differs from Claude JSONL:

- extend `import_session.py`
- add a Codex-specific parsing branch
- add tests against real transcript fixtures

Do not create a framework for this. A simple `if agent == "codex"` branch is fine until proven otherwise.

### Step 3: Keep Automation Small

Use the documented `Stop` hook for capture, but keep the implementation narrow:

- prefer the official stdin payload over scanning `~/.codex/sessions/`
- keep transcript scanning only as legacy fallback
- dedupe by `turn_id` or transcript change, because `Stop` is turn-scoped, not session-end
- rate-limit automatic imports per session so one active Codex thread does not
  produce repetitive rolling summaries every few minutes

### Step 4: Revisit Bigger Architecture Only If Needed

Only consider abstracting `claude_agent_sdk` or adding a real provider layer when all three are true:

1. a second processing backend is real, not hypothetical
2. there is a concrete task worth routing to it
3. the benefit beats the added complexity and cost

Until then, direct `claude_agent_sdk` calls are the correct tradeoff.

## Minimal Code Changes That Still Make Sense

These are worth doing in the near term:

- document the supported Codex import workflow more explicitly
- add transcript fixtures once real Codex examples exist
- keep the Stop-hook implementation aligned with the official payload contract
- keep writing source metadata into daily logs

These are **not** worth doing yet:

- processing-provider registry
- task routers
- backend selection config
- generalized capture adapters

## Decision Rules

Use these rules to avoid overbuilding:

- If one extra `if` solves it, do not add a new module tree.
- If the answer depends on unknown Codex behavior, research first.
- If the change increases API spend, write down the expected benefit first.
- If the feature only makes the system feel more symmetrical, skip it.

## What "Codex Support" Means In This Repo

For this project, Codex support should mean:

- Codex sessions can be brought into the KB reliably
- provenance is preserved
- the workflow is simple enough that one person will actually use it

It does **not** need to mean:

- mirrored hook architecture
- identical runtime semantics
- two interchangeable processing providers

## Trigger For Revisiting Architecture

Re-open the bigger architecture discussion only if one of these becomes true:

- Codex transcript automation becomes trivial and reliable
- the KB grows enough that the current scripts become painful
- OpenAI-backed processing shows a concrete advantage worth paying for

Until then, keep it flat and boring.
