from __future__ import annotations

import os
import json
import signal
import sys
import time
from pathlib import Path

import codex_exec
import pytest
import runtime_config


@pytest.fixture(autouse=True)
def isolated_codex_config(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("MEMORY_CODEX_BIN", raising=False)
    monkeypatch.delenv("MEMORY_CODEX_MODEL", raising=False)
    monkeypatch.setattr(runtime_config, "RUNTIME_CONFIG_FILE", tmp_path / "runtime-config.json")


def write_python_codex(tmp_path: Path, body: str) -> Path:
    executable = tmp_path / "fake-codex"
    executable.write_text(f"#!{sys.executable}\n{body}", encoding="utf-8")
    executable.chmod(0o755)
    return executable


def test_service_can_isolate_interactive_tools_and_reasoning(tmp_path, monkeypatch):
    runtime_config.RUNTIME_CONFIG_FILE.write_text(json.dumps({
        "codex_isolate_config": True, "codex_reasoning_effort": "medium",
    }))
    fake = write_python_codex(tmp_path, "import sys,json\nfrom pathlib import Path\nPath(sys.argv[sys.argv.index('--output-last-message')+1]).write_text(json.dumps(sys.argv))")
    args = json.loads(codex_exec.run_codex_prompt("test", cwd=tmp_path, allow_edits=False, executable=fake))
    assert "--ignore-user-config" in args
    assert "--ignore-rules" in args
    assert 'model_reasoning_effort="medium"' in args


def assert_process_exited(pid: int) -> None:
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return
        time.sleep(0.02)
    pytest.fail(f"Process {pid} is still present after timeout cleanup")


def start_deadline_after_file(monkeypatch, path: Path) -> None:
    """Separate fixture interpreter startup from the deadline under test."""
    original_wait = codex_exec._wait_with_deadline
    def wait(process, timeout):
        startup_deadline = time.monotonic() + 5
        while not path.exists():
            if process.poll() is not None or time.monotonic() >= startup_deadline:
                raise AssertionError(f"Fixture did not become ready: {path.name}")
            time.sleep(0.01)
        return original_wait(process, timeout)
    monkeypatch.setattr(codex_exec, "_wait_with_deadline", wait)


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


@pytest.mark.skipif(os.name != "posix", reason="Uses POSIX signals and process groups")
@pytest.mark.parametrize("parent_exits_first", [False, True])
def test_timeout_stops_and_reaps_process_tree(
    tmp_path: Path, monkeypatch, parent_exits_first: bool
) -> None:
    """Timeout must also kill descendants when the group leader exits on SIGTERM."""
    executable = write_python_codex(
        tmp_path,
        f"""import os, signal, subprocess, sys, time
from pathlib import Path
prompt = sys.stdin.read()
sys.stderr.write(prompt + '\\n')
sys.stderr.flush()
Path('parent.pid').write_text(str(os.getpid()))
output = Path(sys.argv[sys.argv.index('--output-last-message') + 1])
output.write_text('partial result')
Path('output.path').write_text(str(output))
child = subprocess.Popen([sys.executable, '-c',
    "import os, signal, time; from pathlib import Path; "
    "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
    "Path('child.pid').write_text(str(os.getpid())); time.sleep(60)"])
def terminate(signum, frame):
    child.kill()
    child.wait()
    Path('child.reaped').touch()
    raise SystemExit(0)
if not {parent_exits_first!r}:
    signal.signal(signal.SIGTERM, terminate)
time.sleep(60)
""",
    )
    monkeypatch.setenv("MEMORY_CODEX_BIN", str(executable))
    prompt = "private conversation: do not persist this context in errors"
    start_deadline_after_file(monkeypatch, tmp_path / "child.pid")
    started = time.monotonic()
    try:
        with pytest.raises(RuntimeError, match="timed out") as error:
            codex_exec.run_codex_prompt(
                prompt, cwd=tmp_path, allow_edits=False, timeout_seconds=0.5
            )

        assert time.monotonic() - started < 5
        assert prompt not in str(error.value)
        assert_process_exited(int((tmp_path / "parent.pid").read_text()))
        assert_process_exited(int((tmp_path / "child.pid").read_text()))
        assert not Path((tmp_path / "output.path").read_text()).exists()
        if not parent_exits_first:
            assert (tmp_path / "child.reaped").exists()
    finally:
        for name in ("child.pid", "parent.pid"):
            if (tmp_path / name).exists():
                try:
                    os.kill(int((tmp_path / name).read_text()), signal.SIGKILL)
                except ProcessLookupError:
                    pass


@pytest.mark.parametrize("timeout_seconds", [0, -1, float("nan"), float("inf")])
def test_timeout_rejects_invalid_deadlines_before_launch(
    tmp_path: Path, monkeypatch, timeout_seconds: float
) -> None:
    monkeypatch.setenv("MEMORY_CODEX_BIN", str(tmp_path / "not-runnable"))

    with pytest.raises(ValueError, match="positive finite"):
        codex_exec.run_codex_prompt(
            "context", cwd=tmp_path, allow_edits=False, timeout_seconds=timeout_seconds
        )


def test_failure_bounds_diagnostics_and_redacts_prompt(tmp_path: Path, monkeypatch) -> None:
    executable = write_python_codex(
        tmp_path,
        """import json, sys
prompt = sys.stdin.read()
sys.stderr.write('x' * 200_000 + '\\n')
sys.stderr.write('user\\n' + prompt + '\\n')
sys.stderr.write('ERROR rejected input: ' + json.dumps(prompt) + '\\n')
sys.stderr.write('ERROR unsupported model\\n')
raise SystemExit(7)
""",
    )
    monkeypatch.setenv("MEMORY_CODEX_BIN", str(executable))
    prompt = 'private notes "quoted"\nsecond private line'

    with pytest.raises(RuntimeError) as error:
        codex_exec.run_codex_prompt(prompt, cwd=tmp_path, allow_edits=False)

    diagnostic = str(error.value)
    assert "unsupported model" in diagnostic
    assert "7" in diagnostic
    assert len(diagnostic) < 2200
    assert "private" not in diagnostic


def test_failure_handles_non_utf8_stderr(tmp_path: Path, monkeypatch) -> None:
    executable = write_python_codex(
        tmp_path, "import sys\nsys.stderr.buffer.write(b'ERROR unavailable \\xff\\n')\nraise SystemExit(2)\n"
    )
    monkeypatch.setenv("MEMORY_CODEX_BIN", str(executable))

    with pytest.raises(RuntimeError, match="unavailable"):
        codex_exec.run_codex_prompt("context", cwd=tmp_path, allow_edits=False)


def test_timeout_keeps_safe_diagnostic_without_prompt(tmp_path, monkeypatch):
    executable = write_python_codex(tmp_path, """import sys,time
from pathlib import Path
prompt = sys.stdin.read()
sys.stderr.write('ERROR rejected: ' + prompt + '\\nERROR waiting for network\\n')
sys.stderr.flush()
Path('ready').touch()
time.sleep(10)
""")
    start_deadline_after_file(monkeypatch, tmp_path / 'ready')
    with pytest.raises(RuntimeError, match="timed out") as error:
        codex_exec.run_codex_prompt('private conversation', cwd=tmp_path, allow_edits=False,
                                   executable=executable, timeout_seconds=0.3)
    assert "waiting for network" in str(error.value)
    assert "private conversation" not in str(error.value)


def test_diagnostics_redact_runtime_credentials_not_present_in_prompt(tmp_path):
    stderr = tmp_path / "stderr.log"
    stderr.write_text(
        "ERROR Authorization: Bearer runtime-secret-token\n"
        "ERROR connecting to https://service:runtime-password@localhost\n"
    )
    diagnostic = codex_exec._error_diagnostic(stderr, "ordinary request")
    assert "runtime-secret-token" not in diagnostic
    assert "runtime-password" not in diagnostic
    assert "ERROR" in diagnostic


def test_wall_clock_jump_expires_model_deadline(tmp_path, monkeypatch):
    executable = write_python_codex(tmp_path, "import sys,time\nsys.stdin.read()\ntime.sleep(10)")
    started = time.monotonic()
    monkeypatch.setattr(codex_exec, "wall_time",
                        lambda: 1000 if time.monotonic() - started < 0.2 else 5000,
                        raising=False)
    with pytest.raises(RuntimeError, match="timed out"):
        codex_exec.run_codex_prompt('context', cwd=tmp_path, allow_edits=False,
                                   executable=executable, timeout_seconds=2)
    assert time.monotonic() - started < 1.5


def test_clock_rollback_does_not_extend_model_deadline(tmp_path, monkeypatch):
    executable = write_python_codex(tmp_path, "import sys,time\nsys.stdin.read()\ntime.sleep(10)")
    started = time.monotonic()
    monkeypatch.setattr(codex_exec, "wall_time",
                        lambda: 1000 if time.monotonic() - started < 0.1 else 0)
    with pytest.raises(RuntimeError, match="timed out"):
        codex_exec.run_codex_prompt('context', cwd=tmp_path, allow_edits=False,
                                   executable=executable, timeout_seconds=0.4)
    assert time.monotonic() - started < 1.5


def test_deadline_polling_resumes_partial_stdin_without_replaying_prompt(tmp_path):
    executable = write_python_codex(tmp_path, """import sys,time
from pathlib import Path
time.sleep(0.6)
prompt = sys.stdin.read()
Path(sys.argv[sys.argv.index('--output-last-message')+1]).write_text(prompt)
""")
    prompt = 'Large unicode input: Привет!\n' * 20_000
    assert codex_exec.run_codex_prompt(prompt, cwd=tmp_path, allow_edits=False,
                                      executable=executable, timeout_seconds=5) == prompt.strip()


def test_macos_model_call_uses_scoped_idle_sleep_assertion(tmp_path, monkeypatch):
    wrapper = tmp_path / 'caffeinate'
    wrapper.write_text(f'#!{sys.executable}\nimport os,sys\n'
                       'assert sys.argv[1] == "-i"\n'
                       'os.environ["TEST_IDLE_ASSERTION"] = "active"\n'
                       'os.execv(sys.argv[2], sys.argv[2:])\n')
    wrapper.chmod(0o755)
    monkeypatch.setattr(codex_exec, "CAFFEINATE", wrapper, raising=False)
    monkeypatch.setattr(sys, "platform", "darwin")
    executable = write_python_codex(tmp_path, """import os,sys
from pathlib import Path
Path(sys.argv[sys.argv.index('--output-last-message')+1]).write_text(os.environ.get('TEST_IDLE_ASSERTION','missing'))
""")
    assert codex_exec.run_codex_prompt('context', cwd=tmp_path, allow_edits=False,
                                      executable=executable) == 'active'


@pytest.mark.parametrize("explicit_model", [None, "caller-selected-model"])
def test_explicit_executable_and_noninteractive_model_are_used(
    tmp_path: Path, monkeypatch, explicit_model: str | None
) -> None:
    executable = write_python_codex(
        tmp_path,
        """import json, sys
from pathlib import Path
output = Path(sys.argv[sys.argv.index('--output-last-message') + 1])
output.write_text(json.dumps({'model': sys.argv[sys.argv.index('-m') + 1],
    'prompt': sys.stdin.read()}))
""",
    )
    monkeypatch.setenv("MEMORY_CODEX_BIN", str(tmp_path / "wrong-binary"))
    monkeypatch.setenv("MEMORY_CODEX_MODEL", "configured-service-model")
    result = json.loads(codex_exec.run_codex_prompt(
        "context", cwd=tmp_path, allow_edits=False, executable=executable, model=explicit_model
    ))

    assert result == {
        "model": explicit_model or "configured-service-model",
        "prompt": "context",
    }


def test_project_binary_pin_overrides_stale_inherited_service_environment(tmp_path, monkeypatch):
    executable = write_python_codex(tmp_path, "raise SystemExit(0)\n")
    runtime_config.RUNTIME_CONFIG_FILE.write_text(json.dumps({"codex_bin": str(executable)}))
    monkeypatch.setenv("MEMORY_CODEX_BIN", str(tmp_path / "old-service-binary"))

    assert codex_exec.resolve_codex_executable() == str(executable.resolve())


@pytest.mark.parametrize("pin", ["missing", "not-executable", "", 123])
def test_invalid_project_binary_pin_never_falls_back_to_environment(tmp_path, monkeypatch, pin):
    executable = write_python_codex(tmp_path, "raise SystemExit(0)\n")
    monkeypatch.setenv("MEMORY_CODEX_BIN", str(executable))
    configured = str(tmp_path / pin) if pin in ("missing", "not-executable") else pin
    if pin == "not-executable":
        Path(configured).touch()
    runtime_config.RUNTIME_CONFIG_FILE.write_text(json.dumps({"codex_bin": configured}))

    with pytest.raises((RuntimeError, ValueError)):
        codex_exec.resolve_codex_executable()


def test_explicit_executable_has_priority_over_a_broken_project_pin(tmp_path):
    executable = write_python_codex(tmp_path, "raise SystemExit(0)\n")
    runtime_config.RUNTIME_CONFIG_FILE.write_text('{"codex_bin": "missing"}')

    assert codex_exec.resolve_codex_executable(executable) == str(executable.resolve())
