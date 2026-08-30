from __future__ import annotations

import os
from pathlib import Path

import codex_exec


def test_run_codex_prompt_streams_prompt_to_codex_stdin(tmp_path: Path, monkeypatch) -> None:
    """A large lint prompt must not be passed through argv and hit ARG_MAX."""
    fake_codex = tmp_path / "codex"
    fake_codex.write_text(
        """#!/bin/sh
output=""
last=""
while [ "$#" -gt 0 ]; do
    if [ "$1" = "--output-last-message" ]; then
        output="$2"
        shift 2
    else
        last="$1"
        shift
    fi
done
[ "$last" = "-" ] || exit 9
[ "$MEMORY_COMPILER_INTERNAL" = "1" ] || exit 10
cat > "$output"
""",
        encoding="utf-8",
    )
    fake_codex.chmod(0o755)
    monkeypatch.setenv("PATH", f"{tmp_path}{os.pathsep}{os.environ.get('PATH', '')}")

    result = codex_exec.run_codex_prompt(
        "review this deliberately long prompt",
        cwd=tmp_path,
        allow_edits=False,
    )

    assert result == "review this deliberately long prompt"


def test_run_codex_prompt_marks_service_calls_as_internal(tmp_path: Path, monkeypatch) -> None:
    """Internal Codex calls must not re-enter the conversation import hooks."""
    fake_codex = tmp_path / "codex"
    fake_codex.write_text(
        """#!/bin/sh
output=""
while [ "$#" -gt 0 ]; do
    if [ "$1" = "--output-last-message" ]; then
        output="$2"
        shift 2
        continue
    fi
    shift
done
[ "$MEMORY_COMPILER_INTERNAL" = "1" ] || exit 10
printf 'NO_ISSUES' > "$output"
""",
        encoding="utf-8",
    )
    fake_codex.chmod(0o755)
    monkeypatch.setenv("PATH", f"{tmp_path}{os.pathsep}{os.environ.get('PATH', '')}")

    result = codex_exec.run_codex_prompt("lint", cwd=tmp_path, allow_edits=False)

    assert result == "NO_ISSUES"


def test_run_codex_prompt_uses_configured_binary_when_path_is_minimal(
    tmp_path: Path, monkeypatch
) -> None:
    fake_codex = tmp_path / "configured-codex"
    fake_codex.write_text(
        """#!/bin/sh
output=""
last=""
while [ "$#" -gt 0 ]; do
    if [ "$1" = "--output-last-message" ]; then
        output="$2"
        shift 2
    else
        last="$1"
        shift
    fi
done
[ "$last" = "-" ] || exit 9
cat > "$output"
""",
        encoding="utf-8",
    )
    fake_codex.chmod(0o755)
    monkeypatch.setenv("PATH", "/bin")
    monkeypatch.setenv("MEMORY_CODEX_BIN", str(fake_codex))

    result = codex_exec.run_codex_prompt("healthy", cwd=tmp_path, allow_edits=False)

    assert result == "healthy"
