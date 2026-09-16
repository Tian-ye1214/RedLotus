import asyncio
from pathlib import Path

import pytest
from pydantic_ai import Agent
from pydantic_ai.messages import (
    ToolReturnPart,
    ModelRequest,
    UserPromptPart,
    ModelResponse,
    TextPart,
)
from pydantic_ai.models.function import FunctionModel, DeltaToolCall
from pydantic_ai.usage import UsageLimits

from redlotus.core.agents import AgentRunner
from redlotus.core import history as checker
from redlotus.core.agents import WorkspaceContext
from redlotus.core.agents import workspace_context
from redlotus.core.session import SessionFile
from redlotus.core.cli_commands import read_saved_model_messages_file
from redlotus.core.history import messages_safe_for_new_prompt
from redlotus.core.history import ChatHistory
from redlotus.core.agents import AgentRunPolicy
from redlotus.tools.registry import _model_result
from redlotus.tools.registry import tool_result_succeeded


async def run_test_agent(model, *, tools=(), **kwargs):
    kwargs.update(prompt="test", message_history=[], usage_limits=UsageLimits())
    return await AgentRunner().run(
        agent=Agent(FunctionModel(stream_function=model), tools=tools), **kwargs
    )


async def test_large_tool_batch_compacts_before_request_and_retains_original(
    tmp_path, monkeypatch
):
    async def limit(**kwargs):
        return 700

    monkeypatch.setattr(checker, "get_effective_max_context_async", limit)
    monkeypatch.setattr(checker, "get_effective_max_context", lambda **kwargs: 700)
    monkeypatch.setattr(
        checker, "get_model_and_params", lambda role: ("auxiliary", {"max_tokens": 100})
    )
    monkeypatch.setattr(
        checker,
        "get_context_config",
        lambda role: dict(auto_compress_ratio=0.7, head_turns=2, tail_turns=2),
    )
    monkeypatch.setattr(
        checker,
        "_call_compressor_llm",
        lambda **kwargs: "\n\n".join(
            h + "\n保留当前目标与失败证据" for h in checker._COMPRESS_REQUIRED_HEADINGS
        ),
    )
    requests = []

    async def work() -> str:
        return "原始工具结果" * 600

    async def model(messages, info):
        requests.append(list(messages))
        if len(requests) == 1:
            yield {0: DeltaToolCall(name="work", json_args="{}", tool_call_id="a")}
        else:
            yield "done"

    with workspace_context(WorkspaceContext.from_path(tmp_path)):
        log = SessionFile.create(tmp_path / "sessions", WorkspaceContext.from_path(tmp_path).project_id)

        async def save(run):
            log.save_context(run.all_messages(), turn_id="one")

        result = await run_test_agent(
            model,
            tools=[work],
            on_node=save,
            before_request=lambda run, node: checker.prepare_model_request(
                run, node, role="coordinator"
            ),
        )
        assert result.output == "done"
        assert checker.estimate_context_tokens(requests[1]) < 700
        assert messages_safe_for_new_prompt(requests[1]) == requests[1]
        journal = log.path.read_text(encoding="utf-8")
        assert "原始工具结果" * 600 in journal
        messages, _ = read_saved_model_messages_file(log.path)
        assert messages_safe_for_new_prompt(messages) == messages
        restored = ChatHistory()
        restored.set_messages(messages)
        assert "保留当前目标与失败证据" in (restored.compress_summary_state or "")
        summary = next(
            m for m in messages if (m.metadata or {}).get("origin") == "context_summary"
        )
        from pydantic_ai._agent_graph import _clean_message_history

        continued = _clean_message_history(
            [summary, ModelRequest(parts=[UserPromptPart("继续任务")])]
        )
        restored = ChatHistory()
        restored.set_messages(continued)
        assert "保留当前目标与失败证据" in (restored.compress_summary_state or "")


async def test_cancel_closes_outstanding_call_and_saves_partial_results():
    started = asyncio.Event()
    captured = []

    async def work() -> str:
        started.set()
        await asyncio.sleep(60)
        return "should not return"

    async def model(messages, info):
        yield {0: DeltaToolCall(name="work", json_args="{}", tool_call_id="cancel-me")}

    async def save(run):
        captured[:] = run.all_messages()

    task = asyncio.create_task(run_test_agent(model, tools=[work], on_node=save))
    await asyncio.wait_for(started.wait(), 2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert messages_safe_for_new_prompt(captured) == captured
    returns = [
        part
        for message in captured
        for part in message.parts
        if isinstance(part, ToolReturnPart)
    ]
    assert len(returns) == 1 and returns[0].tool_call_id == "cancel-me"
    assert returns[0].content["status"] == "cancelled"


async def test_request_reserves_configured_output_without_lowering_it(monkeypatch):
    from types import SimpleNamespace
    from pydantic_ai.usage import RequestUsage

    target = SimpleNamespace(
        name="test",
        settings={"max_tokens": 700},
        context={
            "max_context_tokens": 1000,
            "auto_compress_ratio": 0.8,
            "default_context_tokens": 1000,
            "head_turns": 1,
            "tail_turns": 1,
        },
    )
    messages = [
        ModelRequest(parts=[UserPromptPart("old facts")]),
        ModelResponse(parts=[TextPart("done")], usage=RequestUsage(input_tokens=500)),
        ModelRequest(parts=[UserPromptPart("继续")]),
    ]
    calls = []

    async def limit(**kwargs):
        return 1000

    async def compress(history, **kwargs):
        calls.append(kwargs)
        history.set_messages(
            [ModelRequest(parts=[UserPromptPart("summary")]), messages[-1]]
        )
        return True

    monkeypatch.setattr(checker, "get_effective_max_context_async", limit)
    monkeypatch.setattr(checker, "compress_history_async", compress)
    result = await checker.compact_request_messages(
        messages, role="coordinator", target=target
    )
    assert len(calls) == 1
    assert result[-1].parts[0].content == "继续"
    assert target.settings["max_tokens"] == 700
    assert target.context["auto_compress_ratio"] == 0.8


def test_request_budget_includes_system_and_tool_schema():
    from pydantic_ai.tools import ToolDefinition

    messages = [
        ModelRequest(parts=[UserPromptPart("x")], instructions="长期系统规则" * 2000)
    ]
    tool = ToolDefinition(
        name="large",
        description="工具说明" * 2000,
        parameters_json_schema={"type": "object"},
    )
    assert checker.estimate_context_tokens(messages) >= 12000
    assert checker.estimate_context_tokens(
        messages, tools=[tool]
    ) > checker.estimate_context_tokens(messages)


def test_full_tool_output_is_retained_and_failure_survives_preview(tmp_path):
    with workspace_context(WorkspaceContext.from_path(tmp_path)):
        original = "x" * 1000 + "\nExit code: 1"
        preview = _model_result(original, AgentRunPolicy(3, 100, 60))
        assert not tool_result_succeeded(preview)
        path = Path(preview.rsplit("Full original tool result: ", 1)[1])
        assert path.read_text(encoding="utf-8") == original


def test_auxiliary_model_uses_shared_fallback_without_changing_model(monkeypatch):
    looked_up = []
    monkeypatch.setattr(
        checker,
        "get_context_config",
        lambda role: (
            {"default_context_tokens": 393216} if role == "coordinator" else {}
        ),
    )
    monkeypatch.setattr(
        checker, "get_model_and_params", lambda role: ("configured-compressor", {})
    )
    monkeypatch.setattr(
        checker, "lookup_model_context", lambda name: looked_up.append(name)
    )
    assert checker.get_effective_max_context(role="compressor") == 393216
    assert looked_up == ["configured-compressor"]
