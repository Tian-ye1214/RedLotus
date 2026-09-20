"""Terminal views responsibilities."""

from __future__ import annotations

import asyncio
import json
from typing import Any

from rich.text import Text
from textual.app import App
from textual.containers import VerticalScroll
from textual.widgets import (
    Button,
    Collapsible,
    OptionList,
    ProgressBar,
    RichLog,
    Static,
)
from textual.widgets.option_list import Option

import redlotus.runtime.resources as _runtime_resources
from redlotus.presentation.output import (
    ContextUsageItem,
    context_usage_renderable,
    model_stream_visible_text,
    render_review_hunk,
    user_text_panel,
)
from redlotus.presentation.panels import build_panel_snapshot, render_panel
from redlotus.presentation.widgets import (
    PREPARING_LABEL,
    READY_LABEL,
    WORKING_FRAMES,
    WORKING_LABEL,
    AgentInput,
    TuiRunMode,
)


class TerminalViews(App[None]):
    def _on_reviews_changed(self) -> None:
        """Update pending review counts on the owning UI thread."""
        self.call_ui(self._update_pending)

    def _pending_hunks(self) -> list:
        return [
            (entry, hunk)
            for entry in self.system.review_store.entries()
            for hunk in entry.hunks
            if hunk.index not in entry.decisions
        ]

    def _update_pending(self) -> None:
        self._pending_count = len(self._pending_hunks())
        self.refresh_status()

    def action_review(self) -> None:
        """Ctrl+R：审查模式临时占用输出区，全量展示所有待审查改动；y/n 决定，完成后还原输出区。"""
        self._review_items = self._pending_hunks()
        if not self._review_items:
            return
        self._review_mode = True
        view = self.query_one("#review-view", OptionList)
        view.clear_options()
        view.add_options(
            Option(render_review_hunk(entry.name, hunk))
            for entry, hunk in self._review_items
        )
        view.border_title = "审查改动 · y 保留 · n 撤销 · ↑↓ 切换 · Esc 退出"
        self.query_one("#output", RichLog).display = False
        view.display = True
        view.highlighted = 0
        view.focus()
        self.refresh_status()

    def _decide_current(self, reject: bool) -> None:
        if not self._review_mode or not self._review_items:
            return
        view = self.query_one("#review-view", OptionList)
        index = view.highlighted
        if index is None:
            return
        entry, hunk = self._review_items[index]
        try:
            applied = self.system.review_store.decide(entry, hunk.index, reject)
        except (OSError, ValueError) as exc:
            from redlotus.presentation.output import print_warning

            print_warning(str(exc))
            return
        if not applied:
            self.action_review()
            return
        if reject:
            notice = json.dumps(
                {"review": "rejected", "file": entry.name, "location": hunk.location},
                ensure_ascii=False,
            )
            if not self.system.add_urgent(notice):
                from pydantic_ai.messages import ModelRequest, UserPromptPart

                history = self.state.history
                history.set_messages([*history.messages,
                    ModelRequest(
                        parts=[UserPromptPart(notice)],
                        metadata={"origin": "user_review"},
                    )
                ])
                self.system._session.queue.submit(
                    lambda: self.system._checkpoint(history, None)
                )
        view.replace_option_prompt_at_index(
            index, render_review_hunk(entry.name, hunk, "undo" if reject else "keep")
        )
        was_last = index == len(self._review_items) - 1
        if was_last and all(hunk.index in entry.decisions for entry, hunk in self._review_items):
            self._exit_review()
            return
        view.highlighted = min(index + 1, len(self._review_items) - 1)
        self.refresh_status()

    def _exit_review(self) -> None:
        self.system.review_store.finish_decided()
        self._review_mode = False
        self._review_items = []
        view = self.query_one("#review-view", OptionList)
        view.clear_options()
        view.display = False
        self.query_one("#output", RichLog).display = True  # 还原输出区（内容原样保留）
        self._pending_count = len(self._pending_hunks())
        self.query_one("#input", AgentInput).focus()
        self.refresh_status()

    async def open_panel(self, *, include_all: bool = False) -> None:
        if self._review_mode:
            self._exit_review()
        self._panel_mode = True
        self._panel_include_all = include_all
        self.query_one("#output", RichLog).display = False
        panel_view = self.query_one("#panel-view", VerticalScroll)
        panel_view.border_title = "工作区总览 · 每 3 秒刷新 · Esc 退出"
        panel_view.display = True
        self._schedule_panel_refresh()
        self._ensure_panel_timer()
        self.query_one("#input", AgentInput).focus()
        self.refresh_status()

    async def _refresh_panel(self) -> None:
        if not self._panel_mode:
            return
        try:
            from redlotus.runtime.context import conversations_root

            identity = (self.system.workspace, self.system.session_key)
            snapshot = await build_panel_snapshot(
                log_root=conversations_root(),
                system=self.system,
                coordinator_history=self.state.history,
                manager_history=getattr(self.system, "_manager_history", None),
                include_all=self._panel_include_all,
                cache=self._panel_cache,
            )
            if not self._panel_mode or identity != (self.system.workspace, self.system.session_key):
                return
            self.query_one("#panel-content", Static).update(render_panel(snapshot))
            self._update_panel_charts(snapshot)
        except Exception as e:
            _runtime_resources.error(f"刷新工作区面板失败: {type(e).__name__}: {e}", exc_info=True)

    def _update_panel_charts(self, snapshot: Any) -> None:
        """Render independent content, API, Agent and plan counters without rebuilding widgets."""
        self._update_content_chart(snapshot)
        self._update_api_usage(snapshot.history)
        self._update_agent_counts(snapshot.runtime)

    def _update_content_chart(self, snapshot) -> None:
        """Show once-counted content and mark measurements with incomplete coverage."""
        content = snapshot.content
        complete = content.complete and not snapshot.history.skipped_count
        total = content.input_tokens + content.output_tokens
        for key, value in (
            ("input", content.input_tokens),
            ("output", content.output_tokens - content.reasoning_tokens),
            ("reasoning", content.reasoning_tokens),
        ):
            bar = self.query_one("#panel-comp-" + key, ProgressBar)
            bar.display = complete and total > 0
            bar.update(total=total or 1, progress=value)
            text = f"{value:,} tokens"
            if key == "input":
                text += "（估算）"
            if key != "input" and content.missing_reasoning_responses:
                text = (f"未知（总输出 {content.output_tokens:,} tokens）" if key == "output"
                        else f"已报告 {content.reasoning_tokens:,} tokens；其余未知")
            elif complete:
                text += f"  {value / total * 100 if total else 0:.1f}%"
            self.query_one("#panel-value-" + key, Static).update(text)
        notes = ["用户输入及引用文本只计一次；不含系统提示词、旧回复和工具结果。"]
        if not complete:
            notes.append("统计不完整，暂不展示完整占比。")
        if content.incomplete_sessions:
            notes.append(f"{content.incomplete_sessions} 个旧会话输入统计不完整；输入仅为已统计部分。")
        if content.unmetered_attachments:
            notes.append(f"未计量附件 {content.unmetered_attachments} 个。")
        if content.missing_reasoning_responses:
            notes.append(f"{content.missing_reasoning_responses} 次响应推理明细未知。")
        if content.missing_usage_responses:
            notes.append(f"{content.missing_usage_responses} 次响应未报告用量。")
        self.query_one("#panel-content-note", Static).update("\n".join(notes))

    def _update_api_usage(self, history) -> None:
        """Keep provider request accounting separate from unique input estimates."""
        self.query_one("#panel-api-usage", Static).update(
            f"输入 {history.input_tokens:,} tokens · 输出 {history.output_tokens:,} tokens（含推理）\n"
            f"输入缓存：命中 {history.cache_hit_tokens:,} · 未命中 {history.cache_miss_tokens:,} · "
            f"未报告 {max(0, history.input_tokens - history.cache_hit_tokens - history.cache_miss_tokens):,} tokens"
        )

    def _update_agent_counts(self, runtime) -> None:
        """Display live Agents separately from the optional planning checklist."""
        self.query_one("#panel-agent-counts", Static).update(
            "暂不可用" if runtime.active_invocations_error else
            Text.assemble((f"Running {runtime.running_agents}", "cyan"), "   ",
                          (f"Queued {runtime.queued_agents}", "yellow"))
        )
        tasks = runtime.tasks
        task_total = tasks.total or 0
        self.query_one("#panel-task-progress", ProgressBar).display = task_total > 0
        self.query_one("#panel-task-progress", ProgressBar).update(
            total=task_total or 1, progress=tasks.completed or 0
        )
        self.query_one("#panel-task-counts", Static).update(Text.assemble(
            (f"✓ Completed {tasks.completed}/{task_total}", "green"), "   ",
            (f"⟳ Running {tasks.running}", "cyan"), "   ",
            (f"✗ Failed {tasks.failed}", "red"), "   ",
            (f"… Pending {tasks.pending}", "yellow"),
        ) if task_total else "暂无计划任务")

    def _schedule_panel_refresh(self) -> None:
        if not self._panel_mode:
            return
        task = self._panel_refresh_task
        if task is not None and not task.done():
            return
        self._panel_refresh_task = asyncio.create_task(self._refresh_panel())

    def _ensure_panel_timer(self) -> None:
        if self._panel_timer is not None:
            return
        self._panel_timer = self.set_interval(3.0, self._schedule_panel_refresh)

    def _stop_panel_timer(self) -> None:
        timer = self._panel_timer
        self._panel_timer = None
        task = self._panel_refresh_task
        self._panel_refresh_task = None
        if task is not None and not task.done():
            task.cancel()
        if timer is not None:
            timer.stop()

    def _exit_panel(self) -> None:
        if not self._panel_mode:
            return
        self._panel_mode = False
        self._stop_panel_timer()
        self.query_one("#panel-view", VerticalScroll).display = False
        self.query_one("#output", RichLog).display = True
        self.query_one("#input", AgentInput).focus()
        self.refresh_status()

    def set_context_usage(self, items: list[ContextUsageItem]) -> None:
        self._display_content("context-usage", context_usage_renderable(items), bool(items))

    def clear_context_usage(self) -> None:
        self._display_content("context-usage", "", False)

    def _display_content(self, widget_id, content, visible=True):
        """Update one auxiliary display and its visibility together."""
        widget = self.query_one("#" + widget_id, Static)
        widget.update(content)
        widget.display = visible

    def refresh_status(self) -> None:
        if not self.is_running:
            return
        self.query_one("#status", Static).update(self._status_text())
        controller = self.controller
        input_box = self.query_one("#input", AgentInput)
        was_disabled = input_box.disabled
        asking = self._ask_future is not None and not self._ask_future.done()
        input_box.disabled = controller.is_transitioning or (
            self._startup_locked and not asking
        )
        if was_disabled and not input_box.disabled:
            input_box.focus()
        self.query_one("#session-load", Button).disabled = (
            self._startup_locked
            or controller.is_transitioning
            or self._active_line_handlers > 0
            or self.system.has_current_turn
        )
        self.query_one("#session-context", Static).update(self._session_context_text())
        if not input_box.disabled and controller.last_rejected_input and self._ask_future is None:
            if not input_box.value:
                input_box.value = controller.last_rejected_input
                controller.last_rejected_input = None

    def _is_working(self) -> bool:
        return bool(
            (self._ask_future is not None and not self._ask_future.done())
            or self._active_line_handlers > 0
            or self.system.has_current_turn
            or self.system._session.queue.pending
        )

    def _mode_chip(self) -> Text:
        label, color = {
            TuiRunMode.REVIEW: (" ⏵ 审查模式 ", "cyan"),
            TuiRunMode.PASS: (" ⏵⏵ 放行模式 ", "green"),
            TuiRunMode.GOAL: (" ◎ 目标模式 ", "yellow"),
        }[self._run_mode]
        return Text(label, style=f"bold black on {color}")

    def _status_text(self) -> Any:
        if self._panel_mode:
            return Text("Panel 总览    ·    每 3 秒刷新    ·    Esc 退出", style="bold")
        if self._review_mode:
            return Text(
                "审查改动中    ·    y 保留    ·    n 撤销    ·    ↑↓ 切换    ·    Esc 退出",
                style="bold",
            )
        text = Text.assemble(self._mode_chip(), "  ")
        if self._startup_locked and not (
            self._ask_future is not None and not self._ask_future.done()
        ):
            text.append(PREPARING_LABEL, style="dim")
            return text
        if not self._is_working():
            self._working_frame = 0
            text.append(READY_LABEL, style="dim")
            if self._run_mode == TuiRunMode.REVIEW and self._pending_count > 0:
                text.append("       ")
                text.append(
                    f" ⚑ 待审查 {self._pending_count} 处 · 按 Ctrl+R 审查 ",
                    style="bold black on yellow",
                )
            return text
        suffix = WORKING_FRAMES[self._working_frame % len(WORKING_FRAMES)]
        self._working_frame += 1
        if self.system.has_current_goal_turn:
            iteration = self.system.current_goal_iteration
            label = f"目标循环第 {iteration} 轮" if iteration else "目标循环"
            text.append(f"{label}{suffix}")
        else:
            text.append(f"{WORKING_LABEL}{suffix}")
        return text

    def _write_user_input(self, value: str, *, title="用户", **style) -> None:
        """Echo an admitted user message or question reply with the same panel style."""
        self.query_one("#output", RichLog).write(
            user_text_panel(value, title, **style),
            scroll_end=True,
        )

    def _refresh_model_stream(self) -> None:
        self._display_content(
            "stream-preview",
            user_text_panel(
                model_stream_visible_text(self._model_stream_text),
                self._model_stream_title,
                text_style="white",
                border_style="cyan",
            ),
            bool(self._model_stream_text),
        )

    def begin_model_stream(self, title: str) -> None:
        self.clear_model_stream()
        self._stream_session = (self.system.session_key, self.system._session.generation)
        self._model_stream_title = title
        self._refresh_model_stream()

    def begin_model_response(self) -> None:
        """Separate successive model requests within the same user turn."""
        self._model_response_count += 1
        self._model_stream_text = ""
        if self._model_stream_thinking:
            self._model_stream_thinking += f"\n\n── 第 {self._model_response_count} 次响应 ──\n"
        self._refresh_model_stream()

    def append_model_stream_delta(self, text: str, kind: str = "text") -> None:
        if not text or self._stream_session != (self.system.session_key, self.system._session.generation):
            return
        if kind == "thinking":
            self._model_stream_thinking += text
            preview = self.query_one("#thinking-preview", Collapsible)
            preview.display = True
            preview.collapsed = False
            preview.title = "思考 · 接收中"
            self.query_one("#thinking-content", Static).update(Text(self._model_stream_thinking))
            self.query_one("#thinking-scroll", VerticalScroll).scroll_end(animate=False)
            return
        self._model_stream_text += text
        if not self._model_stream_title:
            self._model_stream_title = "模型正在回复"
        self._refresh_model_stream()

    def end_model_stream(self, status: str) -> None:
        """Keep received thinking inspectable after the current reply ends."""
        if self._stream_session != (self.system.session_key, self.system._session.generation):
            return
        if status != "已完成" and self._model_stream_text:
            self.query_one("#output", RichLog).write(user_text_panel(
                self._model_stream_text, f"Coordinator · {status}", border_style="yellow",
            ))
        self._model_stream_text = ""
        self._display_content("stream-preview", "", False)
        preview = self.query_one("#thinking-preview", Collapsible)
        preview.title = f"思考 · {status} · 展开查看"
        preview.collapsed = True

    def clear_model_stream(self) -> None:
        self._model_stream_title = ""
        self._model_stream_text = ""
        self._model_stream_thinking = ""
        self._model_response_count = 0
        self._stream_session = None
        self._display_content("stream-preview", "", False)
        self.query_one("#thinking-preview", Collapsible).display = False
        self.query_one("#thinking-content", Static).update("")
