"""Bounded model calls that can inspect a disposable snapshot but cannot edit it."""

from __future__ import annotations

import asyncio
import math
from dataclasses import dataclass
from pathlib import Path

from codex_exec import run_codex_prompt
from runtime_config import get_claude_model, get_codex_model, get_task_runtime


@dataclass(frozen=True)
class ModelResult:
    text: str
    cost_usd: float = 0.0
    runtime: str = ""
    model: str | None = None


async def call_readonly_model(
    prompt: str, *, cwd: Path, task: str, timeout_seconds: float = 1200,
) -> ModelResult:
    """Return the successful final response; callers own runtime serialization."""
    if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
        raise ValueError("timeout_seconds must be a positive finite number")
    runtime = get_task_runtime(task)
    model = get_codex_model() if runtime == "codex" else get_claude_model()
    if runtime == "codex":
        text = await asyncio.to_thread(
            run_codex_prompt, prompt, cwd=cwd, allow_edits=False,
            model=model, timeout_seconds=timeout_seconds,
        )
        return ModelResult(text=text, runtime=runtime, model=model)

    from claude_agent_sdk import ClaudeAgentOptions, ResultMessage, query

    result = None
    try:
        async with asyncio.timeout(timeout_seconds):
            async for message in query(
                prompt=prompt,
                options=ClaudeAgentOptions(
                    cwd=str(cwd), model=model,
                    tools=["Read", "Glob", "Grep"],
                    allowed_tools=["Read", "Glob", "Grep"],
                    setting_sources=[],
                    env={"MEMORY_COMPILER_INTERNAL": "1", "CLAUDE_INVOKED_BY": "memory_compile"},
                    max_turns=40,
                ),
            ):
                if isinstance(message, ResultMessage):
                    result = message
    except TimeoutError:
        raise RuntimeError(f"Claude model call timed out after {timeout_seconds:g}s") from None
    except Exception as exc:
        # SDK diagnostics may echo the complete request; do not persist them.
        raise RuntimeError(f"Claude model call failed ({type(exc).__name__})") from None
    if result is None or result.is_error or result.subtype != "success":
        raise RuntimeError("Claude model call did not produce a successful result")
    if not isinstance(result.result, str) or not result.result.strip():
        raise RuntimeError("Claude model call produced an empty result")
    return ModelResult(
        text=result.result, cost_usd=result.total_cost_usd or 0.0, runtime=runtime, model=model,
    )
