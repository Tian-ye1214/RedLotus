import asyncio
import threading
from types import SimpleNamespace

from pydantic_ai.messages import (
    ModelRequest,
    ModelResponse,
    TextPart,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
)

from redlotus.core import cli_commands
from redlotus.core import history as history_module
from redlotus.core.gateway import AgentRunner
from redlotus.core.history import ChatHistory


def _summary() -> str:
    return "\n\n".join(
        heading + "\n已保留可恢复的工作状态。"
        for heading in history_module._COMPRESS_REQUIRED_HEADINGS
    )


def _context() -> dict:
    return {
        "max_context_tokens": 8000,
        "default_context_tokens": 8000,
        "auto_compress_ratio": 0.8,
        "head_turns": 0,
        "tail_turns": 0,
    }


def _history() -> ChatHistory:
    history = ChatHistory()
    history.set_messages(
        [
            ModelRequest(parts=[UserPromptPart("开始任务")]),
            ModelResponse(parts=[TextPart("已完成第一步")]),
        ]
    )
    return history


def test_history_revision_advances_for_each_context_replacement():
    history = ChatHistory()
    request = ModelRequest(parts=[UserPromptPart("任务")])
    history.update(SimpleNamespace(all_messages=lambda: [request]))
    assert history.revision == 1
    history.set_messages([request])
    assert history.revision == 2
    history.reset()
    assert history.revision == 3


async def test_prepare_compression_returns_detached_candidate_without_mutating_source(
    monkeypatch,
):
    history = _history()
    before = list(history.messages)
    revision = history.revision
    monkeypatch.setattr(
        history_module, "_call_compressor_llm", lambda **kwargs: _summary()
    )

    candidate = await history_module.prepare_compression(
        history, role="coordinator", force=True, retain_tail=False, context=_context()
    )

    assert candidate is not None and candidate is not history
    assert candidate.messages is not history.messages
    assert history.messages == before
    assert history.revision == revision
    assert len(candidate.messages) == 1
    assert candidate.compress_summary_state == _summary()


async def test_async_compression_discards_stale_candidate_after_source_changes(
    monkeypatch,
):
    history = _history()
    before = list(history.messages)
    revision = history.revision
    entered, release = threading.Event(), threading.Event()

    def compress(**kwargs):
        entered.set()
        assert release.wait(timeout=5)
        return _summary()

    monkeypatch.setattr(history_module, "_call_compressor_llm", compress)
    task = asyncio.create_task(
        history_module.compress_history_async(
            history,
            role="coordinator",
            force=True,
            retain_tail=False,
            context=_context(),
        )
    )
    try:
        await asyncio.wait_for(asyncio.to_thread(entered.wait), timeout=5)
        assert history.messages == before
        assert history.revision == revision
        replacement = [ModelRequest(parts=[UserPromptPart("新任务")])]
        history.set_messages(replacement)
        replacement_revision = history.revision
    finally:
        release.set()

    assert await task is False
    assert history.messages == replacement
    assert history.revision == replacement_revision


def test_repair_interrupted_tool_calls_preserves_results_and_is_idempotent():
    messages = [
        ModelRequest(parts=[UserPromptPart("执行工具")]),
        ModelResponse(
            parts=[
                ToolCallPart("saved", {}, tool_call_id="saved-call"),
                ToolCallPart("unfinished", {}, tool_call_id="unfinished-call"),
            ]
        ),
        ModelRequest(
            parts=[
                ToolReturnPart("saved", {"result": "kept"}, tool_call_id="saved-call")
            ]
        ),
    ]

    repaired = history_module.repair_interrupted_tool_calls(messages)

    assert repaired[:-1] == messages
    repair = repaired[-1]
    assert repair.metadata["origin"] == "runtime_control"
    assert repair.metadata["execution_outcome"] == "unknown"
    assert len(repair.parts) == 1
    result = repair.parts[0]
    assert result.tool_call_id == "unfinished-call"
    assert result.content["status"] == "unknown"
    assert result.metadata["origin"] == "runtime_control"
    assert history_module.messages_safe_for_new_prompt(repaired) == repaired
    assert history_module.repair_interrupted_tool_calls(repaired) == repaired


def test_cancelled_tool_call_receipt_is_failed_and_explicitly_unverified():
    messages = [
        ModelResponse(
            parts=[ToolCallPart("pending", {}, tool_call_id="pending-call")]
        )
    ]

    AgentRunner._close_interrupted_calls(messages, [], asyncio.CancelledError())

    receipt = messages[-2]
    result = receipt.parts[0]
    assert result.outcome == "failed"
    assert result.metadata["origin"] == "runtime_control"
    assert result.metadata["execution_outcome"] == "unknown"
    assert result.metadata["interruption_status"] == "cancelled"
    assert result.content["status"] == "cancelled"
    assert result.content["execution_outcome"] == "unknown"


def test_help_escapes_placeholder_angle_brackets_for_markdown(monkeypatch):
    rendered = []
    monkeypatch.setattr(cli_commands, "print_markdown", rendered.append)

    cli_commands.print_cli_help()

    assert "&lt;path&gt;" in rendered[0]
    assert "&lt;role&gt;" in rendered[0]
    assert "<path>" not in rendered[0]


async def test_ltm_show_reads_the_memory_reader_snapshot(monkeypatch):
    rendered = []

    class Reader:
        async def long_term_snapshot(self):
            return {
                "memory": {"body": "保留的长期记忆", "path": "MEMORY.md", "chars": 7},
                "global_records": {"row_count": 1},
            }

    controller = SimpleNamespace(
        system=SimpleNamespace(_memory=SimpleNamespace(reader=Reader()))
    )
    monkeypatch.setattr(
        cli_commands,
        "print_markdown_panel",
        lambda text, *, title: rendered.append((title, text)),
    )

    await cli_commands.SlashCommands(
        controller, SimpleNamespace(history=ChatHistory()), "/LTM show"
    ).run()

    assert len(rendered) == 1
    assert rendered[0][0] == "LTM"
    assert "保留的长期记忆" in rendered[0][1]


def test_stm_snapshot_labels_project_counts_and_current_session_progress():
    rendered = cli_commands._format_stm_snapshot(
        {
            "row_count": 4,
            "observed_turns": 7,
            "consumed_turns": 5,
            "pending_turns": 2,
            "window_turns": 20,
            "overlap_turns": 3,
        }
    )

    assert "项目记录数: 4" in rendered
    assert "当前会话已结束回合: 7" in rendered
    assert "当前会话待处理回合: 2" in rendered


async def test_compress_delegates_once_to_system_control_operation(monkeypatch):
    coordinator, manager = ChatHistory(), ChatHistory()
    calls, rendered = [], []

    class System:
        _manager_history = manager

        async def compress_context(self, history):
            calls.append(history)
            return ["coordinator: 已压缩", "manager: 已压缩"]

    controller = SimpleNamespace(system=System())
    monkeypatch.setattr(
        cli_commands,
        "print_panel",
        lambda text, *, title: rendered.append((title, text)),
    )

    await cli_commands.SlashCommands(
        controller, SimpleNamespace(history=coordinator), "/compress"
    ).compress()

    assert calls == [coordinator]
    assert rendered == [("上下文压缩", "coordinator: 已压缩\nmanager: 已压缩")]
