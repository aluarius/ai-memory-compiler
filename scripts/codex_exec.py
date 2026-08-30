"""Helpers for invoking Codex CLI non-interactively."""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from pathlib import Path


def build_codex_command(
    *,
    cwd: Path,
    allow_edits: bool,
    output_file: Path,
    prompt: str,
    model: str | None = None,
    executable: str = "codex",
) -> list[str]:
    cmd = [
        executable,
        "exec",
        "-C",
        str(cwd),
        "--skip-git-repo-check",
        "--ephemeral",
        "--color",
        "never",
        "--output-last-message",
        str(output_file),
    ]

    if allow_edits:
        cmd.extend(["--sandbox", "workspace-write"])
    else:
        cmd.extend(["-s", "read-only"])

    if model:
        cmd.extend(["-m", model])

    cmd.append("-")
    return cmd


def resolve_codex_executable() -> str:
    """Find Codex in an interactive shell or an explicitly configured service env."""
    configured = os.environ.get("MEMORY_CODEX_BIN")
    if configured:
        executable = Path(configured).expanduser()
        if executable.is_file() and os.access(executable, os.X_OK):
            return str(executable)
        raise RuntimeError(f"Configured Codex executable is not runnable: {executable}")

    executable = shutil.which("codex")
    if executable is None:
        raise RuntimeError("Codex CLI not found in PATH; set MEMORY_CODEX_BIN for services")
    return executable


def run_codex_prompt(
    prompt: str,
    *,
    cwd: Path,
    allow_edits: bool,
    model: str | None = None,
) -> str:
    executable = resolve_codex_executable()

    with tempfile.NamedTemporaryFile(prefix="codex-last-message-", suffix=".txt", delete=False) as tmp:
        output_path = Path(tmp.name)
    with tempfile.NamedTemporaryFile(prefix="codex-stderr-", suffix=".log", delete=False) as err:
        stderr_path = Path(err.name)

    cmd = build_codex_command(
        cwd=cwd,
        allow_edits=allow_edits,
        output_file=output_path,
        prompt=prompt,
        model=model,
        executable=executable,
    )

    try:
        with stderr_path.open("w", encoding="utf-8") as stderr_handle:
            child_env = os.environ.copy()
            child_env["MEMORY_COMPILER_INTERNAL"] = "1"
            completed = subprocess.run(
                cmd,
                cwd=str(cwd),
                input=prompt,
                text=True,
                stdout=subprocess.DEVNULL,
                stderr=stderr_handle,
                env=child_env,
                check=False,
            )
        if completed.returncode != 0:
            stderr = stderr_path.read_text(encoding="utf-8").strip() or "unknown Codex error"
            raise RuntimeError(f"Codex exec failed: {stderr}")

        if output_path.exists():
            return output_path.read_text(encoding="utf-8").strip()
        return ""
    finally:
        output_path.unlink(missing_ok=True)
        stderr_path.unlink(missing_ok=True)
