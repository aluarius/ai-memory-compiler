"""Minimal runtime configuration helpers."""

from __future__ import annotations

import json
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
RUNTIME_CONFIG_FILE = ROOT_DIR / "scripts" / "runtime-config.json"

DEFAULT_RUNTIME_CONFIG = {
    "flush_runtime": "claude",
    "compile_runtime": "claude",
    "query_runtime": "claude",
    "lint_runtime": "claude",
    "codex_model": None,
}

VALID_RUNTIMES = {"claude", "codex"}


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
    config = load_runtime_config()
    model = config.get("codex_model")
    return str(model) if model else None
