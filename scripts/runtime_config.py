"""Minimal runtime configuration helpers."""

from __future__ import annotations

import json
import os
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
RUNTIME_CONFIG_FILE = ROOT_DIR / "scripts" / "runtime-config.json"

DEFAULT_RUNTIME_CONFIG = {
    "flush_runtime": "claude",
    "compile_runtime": "claude",
    "lint_runtime": "claude",
    "codex_model": None,
    # Project pin overrides stale binaries inherited from installed hooks/services.
    "codex_bin": None,
    "codex_isolate_config": False,
    "codex_reasoning_effort": None,
    # Explicit model for claude-runtime LLM calls. Without it the bundled CLI
    # inherits the user's interactive default (e.g. Fable 5 after /model),
    # silently changing pipeline cost/behavior.
    "claude_model": "claude-opus-4-8",
    # "tiered" feeds compile a relevance-selected index slice via kb_db
    # (~7k tokens); "full" is the pre-FTS behavior (whole index, ~33k) —
    # the revert knob if article quality degrades.
    "compile_index_mode": "tiered",
}

VALID_RUNTIMES = {"claude", "codex"}
VALID_INDEX_MODES = {"tiered", "full"}


def load_runtime_config() -> dict:
    if not RUNTIME_CONFIG_FILE.exists():
        return DEFAULT_RUNTIME_CONFIG.copy()

    config = DEFAULT_RUNTIME_CONFIG.copy()
    try:
        raw = json.loads(RUNTIME_CONFIG_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return config

    if not isinstance(raw, dict):
        return config

    config.update(raw)
    return config


def get_task_runtime(task_name: str) -> str:
    config = load_runtime_config()
    runtime = config.get(f"{task_name}_runtime", "claude")
    if runtime not in VALID_RUNTIMES:
        raise ValueError(f"Unsupported runtime '{runtime}' for task '{task_name}'")
    return runtime


def get_codex_model() -> str | None:
    """Resolve the service model without changing the user's interactive config."""
    config = load_runtime_config()
    model = os.environ.get("MEMORY_CODEX_MODEL", config.get("codex_model"))
    if model is None:
        return None
    if not isinstance(model, str) or not model.strip():
        raise ValueError("Codex model must be a non-empty string or null")
    return model


def get_codex_bin() -> str | None:
    """Return the project's explicit CLI pin; executable checks happen at launch."""
    binary = load_runtime_config().get("codex_bin")
    if binary is None:
        return None
    if not isinstance(binary, str) or not binary.strip():
        raise ValueError("Codex binary must be a non-empty string or null")
    return binary


def get_codex_service_options() -> tuple[bool, str | None]:
    """Opt into CLI-local isolation without changing credentials or global config."""
    config = load_runtime_config()
    isolated = config.get("codex_isolate_config", False)
    effort = config.get("codex_reasoning_effort")
    if not isinstance(isolated, bool):
        raise ValueError("codex_isolate_config must be a boolean")
    if effort is not None and effort not in {"minimal", "low", "medium", "high", "xhigh"}:
        raise ValueError("Unsupported codex_reasoning_effort")
    return isolated, effort


def get_compile_index_mode() -> str:
    config = load_runtime_config()
    mode = config.get("compile_index_mode")
    return mode if mode in VALID_INDEX_MODES else "tiered"


def get_claude_model() -> str:
    config = load_runtime_config()
    model = config.get("claude_model") or DEFAULT_RUNTIME_CONFIG["claude_model"]
    return str(model)
