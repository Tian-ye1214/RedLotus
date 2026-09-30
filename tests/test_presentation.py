from types import SimpleNamespace

from redlotus.core.history import ContentTokenStats, ModelUsageSummary, UsageTotals
from redlotus.ui.presentation import (
    DiffKind,
    _collect_history,
    _collect_task_stats,
    compute_line_diff,
    diff_stats,
    format_diff_text,
)
from redlotus.ui.widgets import UsagePanel


def test_memory_clear_accepts_rich_confirmation(monkeypatch):
    import asyncio
    from itertools import product
    from unittest.mock import AsyncMock, Mock
    from pydantic_ai import ToolReturn
    from redlotus.ui import cli_commands

    for scope, confirmed in product(("STM", "LTM"), (False, True)):
        memory = Mock(short_term_snapshot=AsyncMock(return_value={}),
                      clear_short_term=AsyncMock(), clear_long_term=AsyncMock())
        memory.reader.long_term_snapshot = AsyncMock(return_value={})
        system = SimpleNamespace(_memory=memory, wait_for_memory_quiescent=AsyncMock(return_value=True),
            toolkit=SimpleNamespace(ask_user=AsyncMock(return_value=ToolReturn(
                return_value=f"CLEAR {scope}" if confirmed else "cancel", content=["synthetic attachment"]))))
        for name in ("_format_stm_snapshot", "_format_ltm_snapshot", "print_markdown_panel", "print_success"):
            monkeypatch.setattr(cli_commands, name, Mock(return_value="snapshot"))
        command = cli_commands.SlashCommands(SimpleNamespace(system=system), None, f"/{scope} clear")
        asyncio.run(command.memory())
        selected = memory.clear_long_term if scope == "LTM" else memory.clear_short_term
        other = memory.clear_short_term if scope == "LTM" else memory.clear_long_term
        assert selected.await_count == int(confirmed)
        other.assert_not_awaited()


def test_line_diff_retains_replace_delete_insert_order_and_numbers():
    lines = compute_line_diff(
        "keep\nold\nmiddle\nremoved\nend\n",
        "keep\nnew\nmiddle\nend\nadded\n",
    )
    assert [(line.kind, line.old_no, line.new_no, line.text) for line in lines] == [
        (DiffKind.CTX, 1, 1, "keep"),
        (DiffKind.MOD, 2, None, "old"),
        (DiffKind.MOD, None, 2, "new"),
        (DiffKind.CTX, 3, 3, "middle"),
        (DiffKind.DEL, 4, None, "removed"),
        (DiffKind.CTX, 5, 4, "end"),
        (DiffKind.ADD, None, 5, "added"),
    ]
    assert diff_stats(lines) == (1, 1, 1)
    assert format_diff_text(lines, path="notes.txt", stats=(1, 1, 1)).startswith(
        "notes.txt  +1 -1 ~1\n"
    )


def test_task_panel_counts_known_and_pending_statuses():
    tasks = {
        name: SimpleNamespace(status=status)
        for name, status in (
            ("one", "completed"), ("two", "running"),
            ("three", "failed"), ("four", "waiting"),
        )
    }
    stats = _collect_task_stats(SimpleNamespace(tasks=tasks))
    assert (stats.total, stats.completed, stats.running, stats.failed, stats.pending) == (
        4, 1, 1, 1, 1,
    )


def test_history_panel_keeps_agent_category_model_and_content_totals(tmp_path):
    path = tmp_path / "session" / "model_messages.json"
    path.parent.mkdir()
    path.write_text("{}", encoding="utf-8")
    totals = UsageTotals(responses=1, input_tokens=7, output_tokens=3)
    summary = SimpleNamespace(
        meta={"date": "2026-09-26", "topic": "topic", "session_id": "session", "saved_at": "2026-09-26T01:00:00Z"},
        totals=totals,
        content=ContentTokenStats(input_tokens=4, output_tokens=3),
        by_agent={"Coordinator": totals},
        by_category={"ordinary": totals},
        by_model={"model": ModelUsageSummary("model", totals=totals)},
    )
    history, sessions = _collect_history(tmp_path, SimpleNamespace(load=lambda _: summary))
    assert (history.file_count, history.conversation_count) == (1, 1)
    assert history.content.input_tokens == 4
    assert history.by_agent["Coordinator"].responses == 1
    assert history.by_category["ordinary"].input_tokens == 7
    assert history.by_model["model"].output_tokens == 3
    assert (sessions[0].topic, sessions[0].responses) == ("topic", 1)


def test_usage_panel_preserves_incomplete_content_labels():
    class Target:
        display = None

        def update(self, value=None, **kwargs):
            self.value = value if value is not None else kwargs

    targets = {f"#panel-{prefix}-{key}": Target()
               for prefix in ("comp", "value") for key in ("input", "output", "reasoning")}
    targets["#panel-content-note"] = Target()
    panel = SimpleNamespace(query_one=lambda selector, _: targets[selector])
    content = ContentTokenStats(
        input_tokens=10, output_tokens=7, reasoning_tokens=2,
        incomplete_sessions=1, unmetered_attachments=2,
        missing_reasoning_responses=3, missing_usage_responses=4,
    )
    UsagePanel._update_content_chart(panel, SimpleNamespace(content=content, history=SimpleNamespace(skipped_count=0)))
    assert targets["#panel-content-note"].value == "\n".join((
        "用户输入及引用文本只计一次；不含系统提示词、旧回复和工具结果。",
        "统计不完整，暂不展示完整占比。",
        "1 个旧会话输入统计不完整；输入仅为已统计部分。",
        "未计量附件 2 个。",
        "3 次响应推理明细未知。",
        "4 次响应未报告用量。",
    ))
    assert targets["#panel-comp-input"].display is False
    assert targets["#panel-value-output"].value == "未知（总输出 7 tokens）"


def test_history_constructor_restores_summary_without_aliasing_input():
    from pydantic_ai.messages import ModelRequest, TextContent, UserPromptPart
    from redlotus.sessions.context import ChatHistory
    message = ModelRequest(parts=[UserPromptPart([TextContent("summary", metadata={"origin": "context_summary", "summary": "retained"})])])
    original = [message]
    history = ChatHistory(original)
    original.clear()
    assert history.messages == [message]
    assert history.compress_summary_state == "retained"
    assert history.revision == 0
    history.reset()
    assert history.messages == []
    assert history.compress_summary_state is None
    assert history.revision == 1


def test_usage_groups_preserve_missing_values_cache_pricing_and_source():
    from dataclasses import asdict
    from decimal import Decimal
    from pydantic_ai.messages import ModelResponse, TextPart
    from pydantic_ai.usage import RequestUsage
    from redlotus.core.history import ResolvedTokenPrice, summarize_messages
    messages = [
        ModelResponse(parts=[TextPart("result")], model_name="one", usage=RequestUsage(input_tokens=10, output_tokens=6,
            details={"reasoning_tokens": 2, "prompt_cache_hit_tokens": 3, "prompt_cache_miss_tokens": 11}),
            metadata={"role": "worker", "category": "agent"}),
        ModelResponse(parts=[TextPart("unknown usage")], model_name="one", metadata={"role": "worker", "category": "agent"}),
    ]
    summary = summarize_messages(messages, meta={"input_usage": {"input_tokens": 5}},
        price_resolver=lambda name: ResolvedTokenPrice(name, Decimal("0.01"), Decimal("0.02"), "synthetic"))
    expected = dict(responses=2, missing_usage_responses=1, input_tokens=10, output_tokens=6, reasoning_tokens=2,
                    prompt_billable_tokens=14, completion_billable_tokens=6, cache_hit_tokens=3, cache_miss_tokens=11)
    assert asdict(summary.totals) == expected
    assert asdict(summary.by_agent["worker"]) == expected
    assert asdict(summary.by_category["agent"]) == expected
    model = summary.by_model["one"]
    assert model.totals.responses == 1
    assert model.price.source == "synthetic"
    assert model.price.total_usd == Decimal("0.26")
    assert (summary.content.input_tokens, summary.content.output_tokens, summary.content.reasoning_tokens) == (5, 6, 2)
    assert (summary.content.missing_usage_responses, summary.content.missing_reasoning_responses) == (1, 1)


def test_compression_boundaries_wait_for_every_tool_receipt():
    from pydantic_ai.messages import ModelRequest, ModelResponse, ToolCallPart, ToolReturnPart
    from redlotus.core.history import _closed_boundaries
    from redlotus.sessions.context import pending_tool_calls
    messages = [ModelResponse(parts=[ToolCallPart("one", {}, "call-one"), ToolCallPart("two", {}, "call-two")]),
                ModelRequest(parts=[ToolReturnPart("one", "done", "call-one")]),
                ModelRequest(parts=[ToolReturnPart("two", "done", "call-two")])]
    assert _closed_boundaries(messages[:2]) == [0]
    assert set(pending_tool_calls(messages[:2])) == {"call-two"}
    assert _closed_boundaries(messages) == [0, 3]
    assert pending_tool_calls(messages) == {}
