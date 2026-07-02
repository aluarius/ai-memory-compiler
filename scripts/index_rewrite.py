"""Rewrite bloated knowledge/index.md summary cells with batched LLM calls.

The compiler tends to APPEND history to index summaries instead of rewriting
them (128 rows drifted past the 200-char cap before this existed), and
auto-stub rows linger unrefined. Both classes are detected mechanically; the
LLM returns plain `target: summary` lines and the script applies them
deterministically — no file-editing agent involved.

Usage:
    uv run python scripts/index_rewrite.py            # rewrite all targets
    uv run python scripts/index_rewrite.py --dry-run  # list targets only
"""

from __future__ import annotations

import argparse
import asyncio
import re
from pathlib import Path

from codex_exec import run_codex_prompt
from config import INDEX_FILE, KNOWLEDGE_DIR, LLM_LOCK_FILE
from locking import file_lock
from runtime_config import get_claude_model, get_codex_model, get_task_runtime
from utils import INDEX_ROW_RE

ROOT_DIR = Path(__file__).resolve().parent.parent
MAX_SUMMARY_CHARS = 200
STUB_MARKER = "auto-stub: refine summary on next compile"
BATCH_SIZE = 50
ARTICLE_EXCERPT_CHARS = 1500

# `target: summary`, tolerating [[target]] and stray backticks.
_RESPONSE_LINE_RE = re.compile(r"^\[*([\w/-]+?)\]*\s*:\s*(.+)$")


def _article_excerpt(target: str) -> str:
    path = KNOWLEDGE_DIR / f"{target}.md"
    if not path.exists():
        return "(article file missing)"
    text = path.read_text(encoding="utf-8")
    if text.startswith("---"):
        end = text.find("\n---", 3)
        if end != -1:
            text = text[end + 4:]
    return text.strip()[:ARTICLE_EXCERPT_CHARS]


def collect_rewrite_targets() -> list[dict]:
    """Rows needing a rewrite: over-long summaries and auto-stub placeholders."""
    if not INDEX_FILE.exists():
        return []
    targets = []
    for line in INDEX_FILE.read_text(encoding="utf-8").splitlines():
        m = INDEX_ROW_RE.match(line.strip())
        if not m:
            continue
        target, summary, _sources, _updated = m.groups()
        if STUB_MARKER in summary:
            targets.append({
                "target": target, "summary": summary, "kind": "stub",
                "excerpt": _article_excerpt(target),
            })
        elif len(summary) > MAX_SUMMARY_CHARS:
            targets.append({
                "target": target, "summary": summary, "kind": "long", "excerpt": None,
            })
    return targets


def build_rewrite_prompt(targets: list[dict]) -> str:
    blocks = []
    for t in targets:
        if t["kind"] == "stub":
            blocks.append(
                f"### {t['target']} (stub row — write a real summary from this article excerpt)\n"
                f"{t['excerpt']}"
            )
        else:
            blocks.append(
                f"### {t['target']} (current summary is {len(t['summary'])} chars — compress)\n"
                f"{t['summary']}"
            )
    joined = "\n\n".join(blocks)
    return f"""You maintain the index of a personal knowledge base. Rewrite each
index summary below as ONE line of essence: present state only, no appended
history, no dates, at most {MAX_SUMMARY_CHARS} characters.

Rules:
- Output EXACTLY one line per item, format: <target>: <new summary>
- <target> is the id after '###' (e.g. concepts/foo), copied verbatim
- Plain text only: no '|' characters, no [[wikilinks]], no newlines inside a summary
- Keep concrete technical anchors (tool names, error names, key decisions) over generic phrasing
- Output nothing else — no preamble, no code fences

## Items to rewrite

{joined}"""


def parse_rewrite_response(response: str, targets: list[dict]) -> dict[str, str]:
    """Validate LLM output; silently drop anything malformed or unrequested."""
    requested = {t["target"] for t in targets}
    out: dict[str, str] = {}
    for raw in response.splitlines():
        m = _RESPONSE_LINE_RE.match(raw.strip().strip("`"))
        if not m:
            continue
        target, summary = m.group(1), m.group(2).strip()
        if target not in requested:
            continue
        if not summary or len(summary) > MAX_SUMMARY_CHARS or "|" in summary or "[[" in summary:
            continue
        out[target] = summary
    return out


def apply_rewrites(rewrites: dict[str, str]) -> int:
    """Replace summary cells in index.md; other cells untouched. Returns rows changed."""
    if not rewrites or not INDEX_FILE.exists():
        return 0
    lines = INDEX_FILE.read_text(encoding="utf-8").splitlines()
    changed = 0
    for i, line in enumerate(lines):
        m = INDEX_ROW_RE.match(line.strip())
        if not m:
            continue
        target, old_summary, sources, updated = m.groups()
        new_summary = rewrites.get(target)
        if not new_summary or new_summary == old_summary:
            continue
        lines[i] = f"| [[{target}]] | {new_summary} | {sources} | {updated} |"
        changed += 1
    if changed:
        INDEX_FILE.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return changed


async def _call_llm(prompt: str) -> str:
    runtime = get_task_runtime("lint")
    # Serialize against flush/compile — concurrent bundled-CLI instances crash.
    with file_lock(LLM_LOCK_FILE):
        if runtime == "codex":
            return await asyncio.to_thread(
                run_codex_prompt, prompt,
                cwd=ROOT_DIR, allow_edits=False, model=get_codex_model(),
            )
        from claude_agent_sdk import (
            AssistantMessage,
            ClaudeAgentOptions,
            TextBlock,
            query,
        )

        response = ""
        async for message in query(
            prompt=prompt,
            options=ClaudeAgentOptions(
                cwd=str(ROOT_DIR),
                model=get_claude_model(),
                allowed_tools=[],
                max_turns=2,
            ),
        ):
            if isinstance(message, AssistantMessage):
                for block in message.content:
                    if isinstance(block, TextBlock):
                        response += block.text
        return response


async def run_summary_rewrite(batch_size: int = BATCH_SIZE) -> int:
    """Rewrite all over-long/stub index summaries. Returns rows changed.

    Best-effort by contract: an LLM failure logs a warning and returns what
    was already applied — callers (post-compile) must never fail because of
    this pass.
    """
    targets = collect_rewrite_targets()
    if not targets:
        return 0
    print(f"  Index summary rewrite: {len(targets)} target(s)")
    total = 0
    for start in range(0, len(targets), batch_size):
        batch = targets[start:start + batch_size]
        try:
            response = await _call_llm(build_rewrite_prompt(batch))
        except Exception as e:
            print(f"  Warning: summary rewrite LLM call failed: {e}")
            break
        applied = apply_rewrites(parse_rewrite_response(response, batch))
        total += applied
        print(f"  Batch {start // batch_size + 1}: {applied}/{len(batch)} rewritten")
    return total


def main() -> int:
    parser = argparse.ArgumentParser(description="Rewrite bloated index summaries")
    parser.add_argument("--dry-run", action="store_true",
                        help="List rewrite targets without calling the LLM")
    args = parser.parse_args()

    targets = collect_rewrite_targets()
    if args.dry_run:
        for t in targets:
            print(f"{t['kind']:5} {t['target']} ({len(t['summary'])} chars)")
        print(f"{len(targets)} target(s)")
        return 0

    changed = asyncio.run(run_summary_rewrite())
    print(f"Rewrote {changed} index summaries")
    if changed:
        from kb_git import ensure_kb_repo, kb_commit

        ensure_kb_repo()
        kb_commit(f"index summary rewrite ({changed} rows)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
