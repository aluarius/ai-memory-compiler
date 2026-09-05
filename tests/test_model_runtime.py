from __future__ import annotations

import asyncio

import pytest
import claude_agent_sdk


def result_message(subtype="success", is_error=False, result='{"articles": []}'):
    return claude_agent_sdk.ResultMessage(
        subtype=subtype, duration_ms=1, duration_api_ms=1, is_error=is_error,
        num_turns=1, session_id="fixture", total_cost_usd=0.1, result=result,
    )


def test_claude_adapter_restricts_tools_and_returns_final_result(tmp_path, monkeypatch):
    import model_runtime

    monkeypatch.setattr(model_runtime, "get_task_runtime", lambda task: "claude")

    async def query(*, prompt, options):
        assert options.tools == ["Read", "Glob", "Grep"]
        assert options.allowed_tools == ["Read", "Glob", "Grep"]
        assert options.setting_sources == []
        assert options.cwd == str(tmp_path)
        assert options.env["MEMORY_COMPILER_INTERNAL"] == "1"
        yield result_message()

    monkeypatch.setattr(claude_agent_sdk, "query", query)
    result = asyncio.run(model_runtime.call_readonly_model("prompt", cwd=tmp_path, task="compile"))

    assert result.text == '{"articles": []}'
    assert result.cost_usd == 0.1


@pytest.mark.parametrize("message", [
    result_message("error_max_turns", True), result_message(result=None),
])
def test_claude_adapter_rejects_failed_or_missing_result(tmp_path, monkeypatch, message):
    import model_runtime

    monkeypatch.setattr(model_runtime, "get_task_runtime", lambda task: "claude")

    async def query(**kwargs):
        yield message

    monkeypatch.setattr(claude_agent_sdk, "query", query)
    with pytest.raises(RuntimeError):
        asyncio.run(model_runtime.call_readonly_model("prompt", cwd=tmp_path, task="compile"))


def test_claude_adapter_times_out_without_exposing_prompt(tmp_path, monkeypatch):
    import model_runtime

    monkeypatch.setattr(model_runtime, "get_task_runtime", lambda task: "claude")

    async def query(**kwargs):
        await asyncio.sleep(10)
        yield result_message()

    monkeypatch.setattr(claude_agent_sdk, "query", query)
    with pytest.raises(RuntimeError, match="timed out") as error:
        asyncio.run(model_runtime.call_readonly_model(
            "private context", cwd=tmp_path, task="compile", timeout_seconds=0.01,
        ))
    assert "private context" not in str(error.value)
