from __future__ import annotations

import json
from pathlib import Path

from codex_exec import build_codex_command
import runtime_config
import pytest


@pytest.fixture(autouse=True)
def isolated_codex_model(monkeypatch) -> None:
    monkeypatch.delenv("MEMORY_CODEX_MODEL", raising=False)


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
                "lint_runtime": "codex",
                "codex_model": "gpt-5.4",
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(runtime_config, "RUNTIME_CONFIG_FILE", config_path)

    assert runtime_config.get_task_runtime("flush") == "codex"
    assert runtime_config.get_task_runtime("lint") == "codex"
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
    assert "--sandbox" in cmd
    assert "workspace-write" in cmd
    assert "-m" in cmd
    assert cmd[-1] == "-"


def test_get_claude_model_default_and_override(monkeypatch, tmp_path: Path) -> None:
    # No config file -> default pin
    monkeypatch.setattr(runtime_config, "RUNTIME_CONFIG_FILE", tmp_path / "missing.json")
    assert runtime_config.get_claude_model() == "claude-opus-4-8"

    # Override respected
    config_path = tmp_path / "runtime-config.json"
    config_path.write_text(json.dumps({"claude_model": "claude-sonnet-4-6"}), encoding="utf-8")
    monkeypatch.setattr(runtime_config, "RUNTIME_CONFIG_FILE", config_path)
    assert runtime_config.get_claude_model() == "claude-sonnet-4-6"

    # Explicit null falls back to default
    config_path.write_text(json.dumps({"claude_model": None}), encoding="utf-8")
    assert runtime_config.get_claude_model() == "claude-opus-4-8"


def test_get_compile_index_mode_default_and_validation(tmp_path, monkeypatch):
    import runtime_config

    monkeypatch.setattr(runtime_config, "RUNTIME_CONFIG_FILE", tmp_path / "rc.json")
    assert runtime_config.get_compile_index_mode() == "tiered"

    (tmp_path / "rc.json").write_text('{"compile_index_mode": "full"}', encoding="utf-8")
    assert runtime_config.get_compile_index_mode() == "full"

    (tmp_path / "rc.json").write_text('{"compile_index_mode": "bogus"}', encoding="utf-8")
    assert runtime_config.get_compile_index_mode() == "tiered"


def test_codex_service_model_environment_overrides_project_config(tmp_path, monkeypatch) -> None:
    config_path = tmp_path / "runtime-config.json"
    config_path.write_text('{"codex_model": "project-model"}', encoding="utf-8")
    monkeypatch.setattr(runtime_config, "RUNTIME_CONFIG_FILE", config_path)
    monkeypatch.setenv("MEMORY_CODEX_MODEL", "service-model")

    assert runtime_config.get_codex_model() == "service-model"
    assert json.loads(config_path.read_text()) == {"codex_model": "project-model"}


@pytest.mark.parametrize("model", ["", "   ", 42, ["model"]])
def test_codex_model_rejects_invalid_project_values(tmp_path, monkeypatch, model) -> None:
    config_path = tmp_path / "runtime-config.json"
    config_path.write_text(json.dumps({"codex_model": model}), encoding="utf-8")
    monkeypatch.setattr(runtime_config, "RUNTIME_CONFIG_FILE", config_path)

    with pytest.raises(ValueError, match="non-empty string"):
        runtime_config.get_codex_model()


def test_codex_binary_pin_is_optional_project_configuration(tmp_path, monkeypatch):
    config_path = tmp_path / "runtime-config.json"
    monkeypatch.setattr(runtime_config, "RUNTIME_CONFIG_FILE", config_path)
    assert runtime_config.get_codex_bin() is None

    config_path.write_text('{"codex_bin": "/service/codex"}', encoding="utf-8")
    assert runtime_config.get_codex_bin() == "/service/codex"
