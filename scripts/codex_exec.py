"""Helpers for invoking Codex CLI non-interactively."""

from __future__ import annotations

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
) -> list[str]:
    cmd = [
        "codex",
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
        cmd.append("--full-auto")
    else:
        cmd.extend(["-s", "read-only"])

    if model:
        cmd.extend(["-m", model])

    cmd.append(prompt)
    return cmd


def run_codex_prompt(
    prompt: str,
    *,
    cwd: Path,
    allow_edits: bool,
    model: str | None = None,
) -> str:
    if shutil.which("codex") is None:
        raise RuntimeError("Codex CLI not found in PATH")

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
    )

    try:
        with stderr_path.open("w", encoding="utf-8") as stderr_handle:
            completed = subprocess.run(
                cmd,
                cwd=str(cwd),
                stdout=subprocess.DEVNULL,
                stderr=stderr_handle,
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
