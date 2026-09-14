import asyncio
import json
from contextlib import asynccontextmanager

from pydantic_ai import Agent
from pydantic_ai.models.function import FunctionModel, DeltaToolCall
from pydantic_ai.messages import ToolReturnPart
from textual.widgets import Input

from redlotus.API.base import BotBase
from redlotus.agent_core.input_messages import UserMessage
from redlotus.cli.output import set_output_sink
from redlotus.cli.tui import RedLotusTui, AgentInput
from redlotus.tools.memory import ChatHistory
from redlotus.runtime.context import active_workspace
from test_system import configured_system, noop


async def until(predicate):
    async with asyncio.timeout(5):
        while not predicate():
            await asyncio.sleep(0.01)


def configure_cli_hooks(system, monkeypatch, *, preparer=None, enter_workspace=False):
    cli = system._cli_controller
    if preparer == "system":
        monkeypatch.setattr(
            system, "prepare_cli_session", lambda: asyncio.sleep(0, result=())
        )
    elif preparer == "legacy":
        monkeypatch.setattr(cli, "prepare_session", lambda: asyncio.sleep(0, result=()))
    if enter_workspace:
        monkeypatch.setattr(cli, "enter_current_workspace", noop)
    monkeypatch.setattr(cli, "_publish_context_usage", noop)
    monkeypatch.setattr(
        "redlotus.agent_core.cli_controller.app_config.missing_main_api_keys",
        lambda: (),
    )


@asynccontextmanager
async def tui_session(app, system):
    try:
        async with app.run_test(size=(100, 28)) as pilot:
            await pilot.pause()
            yield pilot
    finally:
        set_output_sink(None)
        await system.shutdown()


class LocalBot(BotBase):
    platform_tag = "QQ"
    session_prefix = "qq_"
    SESSION_IDLE_TTL_S = 0


async def test_bot_fifo_stop_and_owner_channel_binding(tmp_path, monkeypatch):
    bot = LocalBot()
    monkeypatch.setattr(
        "redlotus.API.base.settings",
        lambda: {
            "bot": {
                "owner_channels": {"qq": ["123"], "wechat": ["wxid_1"]},
                "session_queue_maxsize": 5,
            }
        },
    )
    assert bot._is_owner_session("private_123")
    assert not bot._is_owner_session("group_123")
    assert not bot._is_owner_session("private_456")
    bot.platform_tag = "WeChat"
    assert bot._is_owner_session("wx_wxid_1")
    assert not bot._is_owner_session("wx_stranger")
    bot.platform_tag = "QQ"
    system = configured_system(tmp_path, monkeypatch)
    bot._session("private_123").agent = system
    monkeypatch.setattr("redlotus.API.base.app_config.reload_config", lambda: None)
    monkeypatch.setattr(
        "redlotus.API.base.app_config.missing_main_api_keys", lambda: ()
    )
    started, hold = asyncio.Event(), asyncio.Event()
    seen, sent = [], []

    async def create(*args, **kwargs):
        async def model(messages, info):
            text = messages[-1].parts[0].content[0]
            seen.append(text)
            if text == "first":
                started.set()
                await hold.wait()
            yield "reply:" + text

        return Agent(FunctionModel(stream_function=model))

    async def reply(text):
        sent.append(text)

    monkeypatch.setattr("redlotus.agent_core.system.create_coordinator_agent", create)
    for text in ("first", "second", "third"):
        await bot.dispatch_user_message("private_123", UserMessage(text=text), reply)
    await asyncio.wait_for(started.wait(), 5)
    await bot.dispatch_user_message("private_123", UserMessage(text="/stop"), reply)
    await asyncio.wait_for(bot._session("private_123").queue.join(), 5)
    assert seen == ["first", "second", "third"]
    assert "reply:first" not in sent
    assert [s for s in sent if s.startswith("reply:")] == [
        "reply:second",
        "reply:third",
    ]
    jobs = [
        json.loads(p.read_text(encoding="utf-8"))
        for p in system._memory.observations.turns.glob("*.json")
    ]
    assert (
        next(j for j in jobs if j["user_inputs"] == ["first"])["status"] == "cancelled"
    )
    assert bot._sessions["private_123"].history.messages
    await bot.release_all_resources_async()


async def test_tui_accepts_fifo_input_while_preparing(tmp_path, monkeypatch):
    system = configured_system(tmp_path, monkeypatch)
    configure_cli_hooks(system, monkeypatch, preparer="system", enter_workspace=True)
    app = RedLotusTui(system)
    app.state.is_first_input = False
    started, release = asyncio.Event(), asyncio.Event()
    inputs = []

    async def create(*args, **kwargs):
        async def model(messages, info):
            text = messages[-1].parts[0].content[0]
            inputs.append(text)
            if len(inputs) == 1:
                started.set()
                await release.wait()
            yield "done"

        return Agent(FunctionModel(stream_function=model))

    monkeypatch.setattr("redlotus.agent_core.system.create_coordinator_agent", create)
    async with tui_session(app, system):
        inp = app.query_one("#input", AgentInput)
        for value in ("第一条", "第二条", "第三条"):
            await app.on_input_submitted(Input.Submitted(inp, value))
        await asyncio.wait_for(started.wait(), 5)
        await until(lambda: len(system._session.queue.pending) == 2)
        release.set()
        await until(lambda: len(inputs) == 3 and not system.has_current_turn)
        assert inputs == ["第一条", "第二条", "第三条"]


async def test_tui_urgent_and_stop_do_not_answer_pending_question(
    tmp_path, monkeypatch
):
    system = configured_system(tmp_path, monkeypatch)
    configure_cli_hooks(system, monkeypatch, preparer="system", enter_workspace=True)

    async def create(*args, **kwargs):
        async def model(messages, info):
            if any(isinstance(p, ToolReturnPart) for p in messages[-1].parts):
                yield "done"
            else:
                yield {
                    0: DeltaToolCall(
                        name="ask_user",
                        json_args='{"question":"Which file?"}',
                        tool_call_id="ask",
                    )
                }

        return Agent(FunctionModel(stream_function=model), tools=[system.ask_user])

    monkeypatch.setattr("redlotus.agent_core.system.create_coordinator_agent", create)
    app = RedLotusTui(system)
    app.state.is_first_input = False
    async with tui_session(app, system):
        inp = app.query_one("#input", AgentInput)
        await app.on_input_submitted(Input.Submitted(inp, "ask me"))
        await until(lambda: app._ask_future is not None)
        answer = app._ask_future
        await app.on_input_submitted(Input.Submitted(inp, "/urgent preserve evidence"))
        await until(
            lambda: (
                bool(system._session._urgent) and system._session._urgent[0][1].done()
            )
        )
        assert system._session._urgent[0][1].result().text == "preserve evidence"
        assert not answer.done()
        await app.on_input_submitted(Input.Submitted(inp, "/stop"))
        await until(lambda: not system.has_current_turn)
        assert answer.cancelled() and app._ask_future is None
        assert "/stop" not in system._session.user_inputs


async def test_status_refresh_after_tui_exit_does_not_query_removed_widgets(
    tmp_path, monkeypatch
):
    system = configured_system(tmp_path, monkeypatch)
    configure_cli_hooks(system, monkeypatch, preparer="system", enter_workspace=True)
    app = RedLotusTui(system)
    async with tui_session(app, system):
        app.refresh_status()
    app.refresh_status()


async def test_goal_iterations_form_one_episode_from_original_user(
    tmp_path, monkeypatch
):
    system = configured_system(tmp_path, monkeypatch)
    prompts = []

    async def create(*args, **kwargs):
        async def model(messages, info):
            prompts.append(messages[-1].parts[0].content[0])
            marker = "CONTINUE" if len(prompts) == 1 else "DONE"
            yield "检查结果<!-- REDLOTUS_GOAL: " + marker + " -->"

        return Agent(FunctionModel(stream_function=model))

    monkeypatch.setattr("redlotus.agent_core.system.create_coordinator_agent", create)
    history = ChatHistory()
    state = system.new_cli_session_state()
    hold = asyncio.Event()
    system._session.queue.submit(hold.wait)
    system._session.queue.submit(
        lambda: system.process_cli_line("下一项任务", state, wait_for_turn=True),
        data="下一项任务",
    )
    await system._run_user_turn(
        "goal",
        UserMessage(text="完成验证"),
        history,
        goal_mode=True,
        conversation_log_hint="goal",
    )
    assert len(prompts) == 2
    jobs = [
        json.loads(p.read_text(encoding="utf-8"))
        for p in system._memory.observations.turns.glob("*.json")
    ]
    assert len(jobs) == 1 and jobs[0]["user_inputs"] == ["完成验证"]
    assert "下一项任务" not in "".join(prompts)
    assert len(system._session.queue.pending) == 1
    await system._session.queue.cancel(discard=True)
    await system.shutdown()


async def test_workspace_switch_waits_for_children_and_preserves_review_subscription(
    tmp_path, monkeypatch
):
    from redlotus.runtime.subagents import SubagentSpec
    from redlotus.agent_core.memory_service import MemoryService

    system = configured_system(tmp_path / "a", monkeypatch)
    monkeypatch.setattr(MemoryService, "process_pending", noop)
    second = tmp_path / "b"
    second.mkdir()
    changed, finished, started = [], asyncio.Event(), asyncio.Event()
    parent = asyncio.get_running_loop()
    store = system.review_store
    store.activate(lambda: changed.append(True))
    old = system.workspace

    async def child():
        parent.call_soon_threadsafe(started.set)
        try:
            await asyncio.sleep(30)
        finally:
            assert active_workspace() == old
            parent.call_soon_threadsafe(finished.set)

    job = asyncio.create_task(
        system._orchestrator.factory.run(SubagentSpec("session", "turn", old), child)
    )
    await asyncio.wait_for(started.wait(), 5)
    handles = system._orchestrator.factory.handles
    await system.switch_workspace(second)
    assert finished.is_set() and all(not h.thread.is_alive() for h in handles)
    assert system.workspace.root == second.resolve()
    assert system._toolkit._base_dir == second.resolve()
    assert system.review_store is store
    store.register(second / "new.py", name="new.py", baseline="", snapshot="x=1")
    assert changed and store.entries()[0].path == second / "new.py"
    await asyncio.gather(job, return_exceptions=True)
    await system.shutdown()


async def test_urgent_after_final_response_is_queued_instead_of_lost(
    tmp_path, monkeypatch
):
    system = configured_system(tmp_path, monkeypatch)
    cli = system._cli_controller
    state = cli.new_session_state()
    state.is_first_input = False
    configure_cli_hooks(system, monkeypatch)
    entered, release = asyncio.Event(), asyncio.Event()
    inputs = []

    async def create(*args, **kwargs):
        async def model(messages, info):
            inputs.append(messages[-1].parts[0].content[0])
            yield "done"

        return Agent(FunctionModel(stream_function=model))

    monkeypatch.setattr("redlotus.agent_core.system.create_coordinator_agent", create)
    finish = system._memory.finish_turn

    async def delayed_finish(*args, **kwargs):
        if len(inputs) == 1:
            entered.set()
            await release.wait()
        await finish(*args, **kwargs)

    monkeypatch.setattr(system._memory, "finish_turn", delayed_finish)
    await cli.process_line("first", state, wait_for_turn=False)
    await asyncio.wait_for(entered.wait(), 5)
    await cli.process_line("/urgent follow-up", state, wait_for_turn=False)
    assert [q[2] for q in system._session.queue.pending] == ["follow-up"]
    release.set()
    await until(lambda: len(inputs) == 2 and not system.has_current_turn)
    assert inputs == ["first", "follow-up"]
    await system.shutdown()


async def test_clear_cancels_input_preparation_before_it_can_start_old_task(
    tmp_path, monkeypatch
):
    system = configured_system(tmp_path, monkeypatch)
    cli, started = system._cli_controller, asyncio.Event()
    state = cli.new_session_state()

    async def title(text):
        started.set()
        await asyncio.sleep(30)
        return "late title"

    monkeypatch.setattr(system, "generate_task_title", title)
    monkeypatch.setattr(cli, "_publish_context_usage", noop)
    task = asyncio.create_task(
        cli._start_user_turn_from_raw_input(
            "old input",
            state,
            wait_for_turn=False,
            references=asyncio.sleep(0, result=[]),
            admission=system._session.admit(system.workspace),
        )
    )
    await asyncio.wait_for(started.wait(), 5)
    await cli.reset_session(state.history)
    await asyncio.gather(task, return_exceptions=True)
    assert task.cancelled() and not cli._preparing
    assert not system.has_current_turn and not state.history.messages
    assert not (tmp_path / "WorkDatabase" / "late title").exists()
    await system.shutdown()


async def test_legacy_cli_keeps_reading_while_model_waits(tmp_path, monkeypatch):
    system = configured_system(tmp_path, monkeypatch)
    cli, started = system._cli_controller, asyncio.Event()
    monkeypatch.setenv("REDLOTUS_LEGACY_CLI", "1")
    configure_cli_hooks(system, monkeypatch, preparer="legacy", enter_workspace=True)
    monkeypatch.setattr(
        system, "generate_task_title", lambda text: asyncio.sleep(0, result="test")
    )
    inputs = []

    async def create(*args, **kwargs):
        async def model(messages, info):
            text = messages[-1].parts[0].content[0]
            inputs.append(text)
            if text == "first":
                started.set()
                await asyncio.sleep(30)
            yield "done"

        return Agent(FunctionModel(stream_function=model))

    monkeypatch.setattr("redlotus.agent_core.system.create_coordinator_agent", create)

    class Repl:
        def __init__(self, **kwargs):
            pass

        async def read_line(self):
            return None

        async def run(self, handler, *, stop_event):
            assert await handler("first") == "continue"
            await asyncio.wait_for(started.wait(), 5)
            assert await handler("second") == "continue"
            await until(lambda: len(system._session.queue.pending) == 1)
            await handler("/stop")
            await until(
                lambda: inputs == ["first", "second"] and not system.has_current_turn
            )
            await handler("/exit")
            await asyncio.wait_for(stop_event.wait(), 5)

    monkeypatch.setattr("redlotus.agent_core.cli_controller.InteractiveRepl", Repl)
    await cli.run_interactive()
    assert inputs == ["first", "second"]
