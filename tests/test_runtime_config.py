from __future__ import annotations

import json
from pathlib import Path

from codex_exec import build_codex_command
import runtime_config


def test_runtime_config_defaults(monkeypatch, tmp_path: Path) -> None:
    config_path = tmp_path / "runtime-config.json"
    monkeypatch.setattr(runtime_config, "RUNTIME_CONFIG_FILE", config_path)

    assert runtime_config.get_task_runtime("flush") == "claude"
    assert runtime_config.get_codex_model() is None


def test_runtime_config_reads_overrides(monkeypatch, tmp_path: Path) -> None:
    config_path = tmp_path / "runtime-config.json"
    config_path.write_text(
        json.dumps(
            {
                "flush_runtime": "codex",
                "query_runtime": "codex",
                "codex_model": "gpt-5.4",
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(runtime_config, "RUNTIME_CONFIG_FILE", config_path)

    assert runtime_config.get_task_runtime("flush") == "codex"
    assert runtime_config.get_task_runtime("query") == "codex"
    assert runtime_config.get_codex_model() == "gpt-5.4"


def test_runtime_config_falls_back_on_invalid_json(monkeypatch, tmp_path: Path) -> None:
    config_path = tmp_path / "runtime-config.json"
    config_path.write_text("{invalid json", encoding="utf-8")
    monkeypatch.setattr(runtime_config, "RUNTIME_CONFIG_FILE", config_path)

    assert runtime_config.get_task_runtime("compile") == "claude"
    assert runtime_config.get_codex_model() is None


def test_build_codex_command_uses_expected_mode() -> None:
    output_file = Path("/tmp/out.txt")
    cmd = build_codex_command(
        cwd=Path("/repo"),
        allow_edits=True,
        output_file=output_file,
        prompt="Hello",
        model="gpt-5.4",
    )

    assert cmd[:2] == ["codex", "exec"]
    assert "--full-auto" in cmd
    assert "-m" in cmd
    assert cmd[-1] == "Hello"
