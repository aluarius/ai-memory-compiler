"""Helpers for invoking Codex CLI non-interactively."""

from __future__ import annotations

import json
import math
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
from pathlib import Path
from time import monotonic, sleep, time as wall_time

from runtime_config import get_codex_bin, get_codex_model, get_codex_service_options

HOOKS_DIR = Path(__file__).resolve().parent.parent / "hooks"
if str(HOOKS_DIR) not in sys.path:
    sys.path.insert(0, str(HOOKS_DIR))
from sanitize import sanitize  # noqa: E402 - reuse the shared credential redactor.

STDERR_READ_LIMIT = 8192
DIAGNOSTIC_LIMIT = 2048
TERMINATION_GRACE_SECONDS = 1.0
CAFFEINATE = Path("/usr/bin/caffeinate")


def _wait_with_deadline(process: subprocess.Popen[str], timeout: float) -> None:
    """Bound both elapsed awake time and wall time, including suspend/resume.

    Stdin is an anonymous file, not a pipe: partial writes cannot stall polling
    on Python versions that cannot resume communicate after an input timeout.
    Clock rollback cannot extend the monotonic deadline.
    """
    wall_deadline = wall_time() + timeout
    monotonic_deadline = monotonic() + timeout
    while True:
        remaining = min(wall_deadline - wall_time(), monotonic_deadline - monotonic())
        if remaining <= 0:
            raise subprocess.TimeoutExpired(process.args, timeout)
        try:
            process.wait(timeout=min(0.25, remaining))
            return
        except subprocess.TimeoutExpired:
            continue


def build_codex_command(
    *,
    cwd: Path,
    allow_edits: bool,
    output_file: Path,
    prompt: str,
    model: str | None = None,
    executable: str = "codex",
    isolate_config: bool = False,
    reasoning_effort: str | None = None,
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

    if isolate_config:
        cmd.extend(["--ignore-user-config", "--ignore-rules"])
    if reasoning_effort:
        cmd.extend(["-c", f"model_reasoning_effort={json.dumps(reasoning_effort)}"])

    cmd.append("-")
    return cmd


def resolve_codex_executable(executable: str | Path | None = None) -> str:
    """Resolve caller > project pin > inherited service env > interactive PATH."""
    # Installed hooks can retain an old MEMORY_CODEX_BIN after the interactive CLI
    # is upgraded. A project pin repairs that without rewriting global hooks.
    configured = executable if executable is not None else get_codex_bin()
    if configured is None:
        configured = os.environ.get("MEMORY_CODEX_BIN")
    if configured is not None:
        configured_path = Path(configured).expanduser().resolve()
        if configured_path.is_file() and os.access(configured_path, os.X_OK):
            return str(configured_path)
        raise RuntimeError(f"Configured Codex executable is not runnable: {configured_path}")

    executable = shutil.which("codex")
    if executable is None:
        raise RuntimeError("Codex CLI not found in PATH; set MEMORY_CODEX_BIN for services")
    return executable


def _signal_process_group(pid: int, signum: int) -> None:
    """Retry Darwin's exiting-group EPERM race; never ignore persistent denial."""
    deadline = monotonic() + TERMINATION_GRACE_SECONDS
    while True:
        try:
            os.killpg(pid, signum)
            return
        except ProcessLookupError:
            return
        except PermissionError:
            # XNU skips zombies while signalling a group and can return EPERM
            # until the last group member is reaped. Other denials remain errors.
            if sys.platform != "darwin" or monotonic() >= deadline:
                raise
            sleep(0.01)


def _terminate_process_group(process: subprocess.Popen[str]) -> None:
    """Stop this invocation's descendants and reap its direct child."""
    if os.name == "posix":
        _signal_process_group(process.pid, signal.SIGTERM)
        try:
            process.wait(timeout=TERMINATION_GRACE_SECONDS)
        except subprocess.TimeoutExpired:
            pass
        finally:
            # The leader may exit while a descendant ignores SIGTERM.
            _signal_process_group(process.pid, signal.SIGKILL)
    else:
        try:
            subprocess.run(
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=5,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            pass
        if process.poll() is None:
            process.kill()
    process.wait()


def _error_diagnostic(stderr_path: Path, prompt: str) -> str:
    """Read a bounded error tail, excluding normal CLI output and prompt echoes."""
    with stderr_path.open("rb") as stderr:
        size = stderr.seek(0, os.SEEK_END)
        offset = max(0, size - STDERR_READ_LIMIT)
        stderr.seek(offset)
        tail = stderr.read(STDERR_READ_LIMIT)
    if offset:
        # A partial first line may contain a truncated fragment of the prompt.
        tail = tail.partition(b"\n")[2]
    text = tail.decode("utf-8", errors="replace")
    text = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", text)

    fragments = {prompt, *prompt.splitlines()}
    for fragment in sorted(fragments, key=len, reverse=True):
        if fragment:
            text = text.replace(fragment, "[prompt redacted]")
            for ensure_ascii in (False, True):
                escaped = json.dumps(fragment, ensure_ascii=ensure_ascii)[1:-1]
                text = text.replace(escaped, "[prompt redacted]")

    lines = [
        line.strip() for line in text.splitlines()
        if re.search(r"\b(?:error|fatal)\b", line, flags=re.IGNORECASE)
    ]
    diagnostic = sanitize("\n".join(lines)) or "no safe error diagnostic available"
    return diagnostic[-DIAGNOSTIC_LIMIT:]


def run_codex_prompt(
    prompt: str,
    *,
    cwd: Path,
    allow_edits: bool,
    model: str | None = None,
    timeout_seconds: float = 1200,
    executable: str | Path | None = None,
) -> str:
    """Run Codex with a finite deadline, without persisting interactive settings."""
    if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
        raise ValueError("timeout_seconds must be a positive finite number")
    executable = resolve_codex_executable(executable)
    selected_model = model if model is not None else get_codex_model()
    isolated, effort = get_codex_service_options()

    with tempfile.TemporaryDirectory(prefix="codex-exec-") as temp_dir:
        output_path = Path(temp_dir) / "last-message.txt"
        stderr_path = Path(temp_dir) / "stderr.log"
        cmd = build_codex_command(
            cwd=cwd,
            allow_edits=allow_edits,
            output_file=output_path,
            prompt=prompt,
            model=selected_model,
            executable=executable,
            isolate_config=isolated,
            reasoning_effort=effort,
        )
        if sys.platform == "darwin" and CAFFEINATE.is_file():
            # Scoped to this process group; never changes global power settings
            # or keeps the display awake. Forced sleep can still interrupt work.
            cmd = [str(CAFFEINATE), "-i", *cmd]
        with tempfile.TemporaryFile() as stdin_handle, stderr_path.open("w", encoding="utf-8") as stderr_handle:
            # Anonymous, seekable input avoids pipe backpressure during deadline
            # polling, keeps prompts out of argv, and disappears on close.
            stdin_handle.write(prompt.encode("utf-8"))
            stdin_handle.seek(0)
            child_env = os.environ.copy()
            child_env["MEMORY_COMPILER_INTERNAL"] = "1"
            with subprocess.Popen(
                cmd,
                cwd=str(cwd),
                stdin=stdin_handle,
                text=True,
                encoding="utf-8",
                stdout=subprocess.DEVNULL,
                stderr=stderr_handle,
                env=child_env,
                start_new_session=os.name == "posix",
                creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0,
            ) as process:
                try:
                    _wait_with_deadline(process, timeout_seconds)
                except subprocess.TimeoutExpired:
                    _terminate_process_group(process)
                    diagnostic = _error_diagnostic(stderr_path, prompt)
                    raise RuntimeError(
                        f"Codex exec timed out after {timeout_seconds:g}s: {diagnostic}"
                    ) from None
                except BaseException:
                    _terminate_process_group(process)
                    raise
        if process.returncode != 0:
            diagnostic = _error_diagnostic(stderr_path, prompt)
            raise RuntimeError(f"Codex exec failed (exit {process.returncode}): {diagnostic}")

        if output_path.exists():
            return output_path.read_text(encoding="utf-8").strip()
        return ""
