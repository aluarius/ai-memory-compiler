from __future__ import annotations

import maintenance


def test_maintenance_returns_failure_and_notifies_when_any_step_failed(monkeypatch) -> None:
    calls = []
    notices = []
    def step(name: str, cmd: list[str], **kwargs) -> int:
        calls.append((name, cmd))
        return 1 if name == "lint-fix" else 0
    monkeypatch.setattr(maintenance, "run_step", step)
    monkeypatch.setattr(maintenance, "notify", lambda *args: notices.append(args))
    assert maintenance.main([]) == 1
    assert notices
    assert "--strict" in next(cmd for name, cmd in calls if name == "health")


def test_maintenance_success_and_no_notify(monkeypatch) -> None:
    monkeypatch.setattr(maintenance, "run_step", lambda *args, **kwargs: 0)
    def unexpected(*args) -> None:
        raise AssertionError("Notification was disabled")
    monkeypatch.setattr(maintenance, "notify", unexpected)
    assert maintenance.main(["--no-notify"]) == 0


def test_maintenance_health_attention_is_failure(monkeypatch) -> None:
    monkeypatch.setattr(maintenance, "run_step", lambda name, *args, **kwargs: 1 if name == "health" else 0)
    assert maintenance.main(["--no-notify"]) == 1
