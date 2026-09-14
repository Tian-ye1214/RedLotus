import asyncio

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
from pydantic_ai.models.function import FunctionModel, DeltaToolCall
from pydantic_ai.usage import UsageLimits

from redlotus.agent_core.runner import AgentRunner
from redlotus.tools.memory.chat_history import messages_safe_for_new_prompt


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
