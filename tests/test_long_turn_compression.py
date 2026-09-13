from types import SimpleNamespace

from pydantic_ai.messages import (
    ModelRequest,
    ModelResponse,
    TextPart,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
)

from redlotus.ModelGateway import ModelChecker as checker
from redlotus.tools.memory.chat_history import messages_safe_for_new_prompt


async def test_compression_can_summarize_completed_steps_within_a_long_turn(
    monkeypatch,
):
    target = SimpleNamespace(
        name="fixture",
        settings={"max_tokens": 2000},
        context={
            "max_context_tokens": 8000,
            "default_context_tokens": 8000,
            "auto_compress_ratio": 0.8,
            "head_turns": 2,
            "tail_turns": 2,
        },
    )
    messages = []
    for text in ("开始", "约束", "过去事项", "已交付"):
        messages.extend(
            [
                ModelRequest(parts=[UserPromptPart(text)]),
                ModelResponse(parts=[TextPart("完成")]),
            ]
        )
    original = "参考资料" * 2000
    messages.extend(
        [
            ModelRequest(parts=[UserPromptPart(original)]),
            ModelResponse(parts=[ToolCallPart("inspect", {}, tool_call_id="call-1")]),
            ModelRequest(
                parts=[ToolReturnPart("inspect", "实际核验完成", tool_call_id="call-1")]
            ),
        ]
    )

    async def capacity(**kwargs):
        return 8000

    monkeypatch.setattr(checker, "get_effective_max_context_async", capacity)
    monkeypatch.setattr(checker, "get_effective_max_context", lambda **kwargs: 8000)
    monkeypatch.setattr(
        checker,
        "_call_compressor_llm",
        lambda **kwargs: "\n\n".join(
            h + "\n当前任务仍需交付，资料已核验。"
            for h in checker._COMPRESS_REQUIRED_HEADINGS
        ),
    )
    monkeypatch.setattr(
        checker, "_save_compress_debug_artifacts", lambda **kwargs: None
    )
    compacted = await checker.compact_request_messages(
        messages, role="coordinator", target=target
    )
    assert checker.estimate_context_tokens(compacted) < 6000
    assert messages_safe_for_new_prompt(compacted) == compacted
    assert messages[8].parts[0].content == original
