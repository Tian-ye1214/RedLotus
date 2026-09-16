import asyncio

import pytest

from pydantic_ai import Agent
from pydantic_ai.messages import (
    ModelRequest,
    ModelResponse,
    ToolCallPart,
    ToolReturnPart,
    RetryPromptPart,
    TextPart,
    UserPromptPart,
)
from pydantic_ai.models.function import FunctionModel, DeltaToolCall, DeltaThinkingPart
from pydantic_ai.exceptions import UnexpectedModelBehavior
from pydantic_ai.usage import UsageLimits

from redlotus.core.agents import AgentRunner
from redlotus.core.gateway import create_agent
from redlotus.core.gateway import create_function_toolset
from redlotus.core.history import messages_safe_for_new_prompt


async def test_parallel_results_and_urgent_share_next_request():
    requests = []
    urgent = []
    both_started = asyncio.Event()
    started = []

    async def work(name: str) -> str:
        started.append(name)
        if len(started) == 2:
            both_started.set()
        await asyncio.wait_for(both_started.wait(), 1)
        if name == "slow":
            await asyncio.sleep(0.04)
        else:
            urgent.append("请优先检查错误")
        return name

    async def model(messages, info):
        requests.append(list(messages))
        if len(requests) == 1:
            yield {
                0: DeltaToolCall(
                    name="work", json_args='{"name":"slow"}', tool_call_id="a"
                ),
                1: DeltaToolCall(
                    name="work", json_args='{"name":"fast"}', tool_call_id="b"
                ),
            }
        else:
            yield "完成"

    async def take_urgent():
        values = urgent[:]
        urgent.clear()
        return values

    result = await AgentRunner().run(
        agent=Agent(FunctionModel(stream_function=model), tools=[work]),
        prompt="执行",
        message_history=[],
        usage_limits=UsageLimits(),
        take_urgent=take_urgent,
    )
    assert result.output == "完成"
    parts = requests[1][-1].parts
    assert [p.tool_call_id for p in parts if isinstance(p, ToolReturnPart)] == [
        "b",
        "a",
    ]
    assert [p.content for p in parts if isinstance(p, UserPromptPart)] == [
        "请优先检查错误"
    ]


async def test_urgent_during_final_response_is_consumed():
    urgent = []
    requests = []

    async def model(messages, info):
        requests.append(list(messages))
        if len(requests) == 1:
            urgent.append("补充信息")
        yield str(len(requests))

    async def take_urgent():
        values, urgent[:] = urgent[:], []
        return values

    result = await AgentRunner().run(
        agent=Agent(FunctionModel(stream_function=model)),
        prompt="开始",
        message_history=[],
        usage_limits=UsageLimits(),
        take_urgent=take_urgent,
    )
    assert result.output == "2"
    assert requests[1][-1].parts[0].content == "补充信息"


def test_retry_closes_tool_call():
    messages = [
        ModelRequest(parts=[UserPromptPart("test")]),
        ModelResponse(parts=[ToolCallPart("work", {}, "a")]),
        ModelRequest(
            parts=[RetryPromptPart("try again", tool_name="work", tool_call_id="a")]
        ),
        ModelResponse(parts=[TextPart("done")]),
    ]
    assert messages_safe_for_new_prompt(messages) == messages


async def test_thinking_only_after_tools_fails_then_resumes_without_repeating_tools():
    calls = []
    requests = []

    async def write_artifact() -> str:
        calls.append("written")
        return "artifact saved; verification passed"

    async def model(messages, info):
        requests.append(list(messages))
        if len(requests) == 1:
            yield "I will write the artifact now."
            yield {
                0: DeltaToolCall(
                    name="write_artifact", json_args="{}", tool_call_id="write"
                )
            }
        elif len(requests) == 2:
            yield {0: DeltaThinkingPart(content="...", signature=None)}
        else:
            yield "The artifact was saved and verified."

    agent = create_agent(
        FunctionModel(stream_function=model),
        toolsets=[create_function_toolset([write_artifact])],
    )
    saved = []

    async def on_node(run):
        saved[:] = run.all_messages()

    with pytest.raises(UnexpectedModelBehavior):
        await AgentRunner().run(
            agent=agent,
            prompt="Write and verify the artifact, then report the outcome.",
            message_history=[],
            usage_limits=UsageLimits(),
            on_node=on_node,
        )
    assert calls == ["written"]
    assert len(requests) == 2
    assert saved[-1].metadata["status"] == "failed"
    result = await AgentRunner().run(
        agent=agent,
        prompt="Continue with the final report using the existing results.",
        message_history=saved,
        usage_limits=UsageLimits(),
    )
    assert result.output == "The artifact was saved and verified."
    assert calls == ["written"]
    assert len(requests) == 3
    assert any(
        isinstance(part, ToolReturnPart) and part.tool_call_id == "write"
        for message in requests[2]
        for part in message.parts
    )


async def test_thinking_only_cannot_reuse_a_previous_turn_as_success():
    async def model(messages, info):
        yield {0: DeltaThinkingPart(content="...", signature=None)}

    history = [
        ModelRequest(parts=[UserPromptPart("Earlier task")]),
        ModelResponse(parts=[TextPart("Earlier task completed.")]),
    ]
    saved = []

    async def on_node(run):
        saved[:] = run.all_messages()

    with pytest.raises(UnexpectedModelBehavior):
        await AgentRunner().run(
            agent=create_agent(FunctionModel(stream_function=model)),
            prompt="A different task",
            message_history=history,
            usage_limits=UsageLimits(),
            on_node=on_node,
        )
    assert saved[-1].metadata["status"] == "failed"
