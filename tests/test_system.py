import asyncio
import json
import threading

from pydantic_ai import Agent
from pydantic_ai.models.function import FunctionModel, DeltaToolCall

from redlotus.agent_core.input_messages import UserMessage
from redlotus.agent_core.system import AgentSystem
from redlotus.runtime.context import WorkspaceContext
from redlotus.tools.memory import ChatHistory
from redlotus.tools.memory.ltm import LongTermMemory
from redlotus.tools.ManagementTools import TaskManager, TaskStatus
from redlotus.runtime.worker_result import SubagentResult


async def noop(*args, **kwargs):
    pass


def configured_system(tmp_path, monkeypatch):
    system = AgentSystem(workspace=WorkspaceContext.from_path(tmp_path))
    system._memory.long_term = LongTermMemory(tmp_path / "global")
    system._context_prewarmed = True
    monkeypatch.setattr(system, "_sync_skills_for_user_turn", noop)
    monkeypatch.setattr(system._memory, "schedule_processing", lambda **kwargs: None)
    return system


async def test_system_serializes_turns_freezes_session_memory_and_records_raw(
    tmp_path, monkeypatch
):
    system = configured_system(tmp_path, monkeypatch)
    inputs, injections = [], []
    active = 0

    async def create(
        skills, memory, routing, tools, task_state=None, *, instructions=None
    ):
        injections.append(memory)

        async def model(messages, info):
            nonlocal active
            active += 1
            assert active == 1
            inputs.append(messages[-1].parts[0].content[0])
            await asyncio.sleep(0.02)
            if len(inputs) == 1:
                system._memory.long_term.path.write_text(
                    system._memory.long_term.read() + "\n偏好中文", encoding="utf-8"
                )
            yield "done"
            active -= 1

        return Agent(FunctionModel(stream_function=model))

    monkeypatch.setattr("redlotus.agent_core.system.create_coordinator_agent", create)
    history = ChatHistory()
    await asyncio.gather(
        *(
            system.run_agent_system(UserMessage(text=text), history)
            for text in ("第一条", "第二条", "第三条")
        )
    )
    assert inputs == ["第一条", "第二条", "第三条"]
    assert len(injections) == 1
    assert "偏好中文" not in injections[0]
    assert system._memory.injection_for_session() == injections[0]
    assert len(system._memory.observations.order()) == 3
    journals = list(system._memory.observations.root.parent.glob("*.jsonl"))
    rows = [
        json.loads(line)
        for line in journals[0].read_text(encoding="utf-8").splitlines()
    ]
    assert [
        p["content"][0]
        for row in rows
        for p in row["message"]["parts"]
        if p["part_kind"] == "user-prompt"
    ] == inputs
    await system._cli_controller.reset_session(history)
    await system.run_agent_system(UserMessage(text="新会话"), history)
    assert len(injections) == 2 and "偏好中文" in injections[1]
    await system.shutdown()


async def test_missing_current_reply_is_recorded_as_a_failed_turn(tmp_path, monkeypatch):
    import pytest
    from pydantic_ai.exceptions import UnexpectedModelBehavior
    from pydantic_ai.models.function import DeltaThinkingPart
    from redlotus.ModelGateway.agent_factory import create_agent

    system = configured_system(tmp_path, monkeypatch)
    requests = 0

    async def model(messages, info):
        nonlocal requests
        requests += 1
        if requests == 1:
            yield "Earlier task completed."
        else:
            yield {0: DeltaThinkingPart(content="...", signature=None)}

    async def create(*args, **kwargs):
        return create_agent(FunctionModel(stream_function=model))

    monkeypatch.setattr("redlotus.agent_core.system.create_coordinator_agent", create)
    history = ChatHistory()
    try:
        await system.run_agent_system(UserMessage(text="Earlier task"), history)
        with pytest.raises(UnexpectedModelBehavior):
            await system.run_agent_system(UserMessage(text="Different task"), history)
        events = system._memory.observations.read(system._memory.observations.order())
        assert [event.status for event in events] == ["success", "failed"]
        assert events[-1].error
        assert history.messages[-1].metadata["status"] == "failed"
        assert requests == 2
    finally:
        await system.shutdown()


async def test_shutdown_drains_resources_when_the_first_waiter_is_cancelled(
    tmp_path, monkeypatch
):
    import pytest
    from redlotus.runtime.subagents import SubagentSpec

    system = configured_system(tmp_path, monkeypatch)
    started, release = asyncio.Event(), asyncio.Event()

    async def pending(*args, **kwargs):
        started.set()
        await release.wait()

    system._spawn_background(pending())
    first = asyncio.create_task(system.shutdown())
    await asyncio.wait_for(started.wait(), 3)
    first.cancel()
    await asyncio.gather(first, return_exceptions=True)
    release.set()
    await system.shutdown()
    with pytest.raises(asyncio.CancelledError):
        await system._orchestrator.factory.run(
            SubagentSpec("closed", None, system.workspace), noop
        )


async def test_display_transformation_preserves_the_model_response_for_replay(
    tmp_path, monkeypatch
):
    system = configured_system(tmp_path, monkeypatch)
    raw = "verified answer\n[internal completion marker]"

    async def create(*args, **kwargs):
        async def model(messages, info):
            yield raw

        return Agent(FunctionModel(stream_function=model))

    monkeypatch.setattr("redlotus.agent_core.system.create_coordinator_agent", create)
    history, shown = await system.run_agent_system(
        UserMessage(text="Complete this task"),
        ChatHistory(),
        output_transform=lambda output: output.splitlines()[0],
    )
    assert shown == "verified answer"
    assert history.messages[-1].parts[0].content == raw
    await system.shutdown()


async def test_subagent_creation_and_model_run_are_inside_child_thread(
    tmp_path, monkeypatch
):
    system = configured_system(tmp_path, monkeypatch)
    await system.bind_session("session")
    system._session_logs.ensure("child")
    parent_thread = threading.get_ident()
    threads = []

    async def model(messages, info):
        threads.append(threading.get_ident())
        tool = info.output_tools[0]
        yield {
            0: DeltaToolCall(
                name=tool.name,
                json_args='{"status":"success","summary":"verified test"}',
                tool_call_id="out",
            )
        }

    def create(name, params=None, **kwargs):
        assert threading.get_ident() != parent_thread
        return Agent(
            FunctionModel(stream_function=model), output_type=kwargs["output_type"]
        )

    import importlib

    monkeypatch.setattr(
        importlib.import_module("redlotus.tools.WorkerOrchestrator"),
        "create_agent",
        create,
    )
    monkeypatch.setattr(
        "redlotus.ModelGateway.ModelChecker.prepare_model_request", noop
    )
    success, output = await system._orchestrator.execute_task_with_worker(
        "test", turn_id="turn"
    )
    assert success and json.loads(output)["status"] == "success"
    assert all(t != parent_thread for t in threads)
    assert not system._orchestrator.factory.handles
    await system.shutdown()


async def test_cli_fifo_does_not_merge_inputs(tmp_path, monkeypatch):
    system = configured_system(tmp_path, monkeypatch)
    cli = system._cli_controller
    state = cli.new_session_state()
    calls = []

    async def start(text, state, **kwargs):
        calls.append(text)

    monkeypatch.setattr(cli, "_start_user_turn_from_raw_input", start)
    monkeypatch.setattr(
        "redlotus.agent_core.cli_controller.app_config.missing_main_api_keys",
        lambda: (),
    )
    await cli.process_line("first", state, wait_for_turn=False)
    await cli.process_line("second", state, wait_for_turn=False)
    await system._session.queue.join()
    assert calls == ["first", "second"]
    await system.shutdown()


def test_todo_updates_preserve_results_and_reject_invalid_batch():
    manager = TaskManager()
    manager.create_todo_list('[{"id":"a","description":"first"}]')
    manager.mark_task_complete("a", "verified result")
    manager.create_todo_list(
        '[{"id":"a","description":"first"},{"id":"b","description":"second","dependencies":["a"]}]'
    )
    assert manager.tasks["a"].status == TaskStatus.COMPLETED
    assert manager.tasks["a"].result == "verified result"
    assert [t.id for t in manager.get_all_ready_tasks()] == ["b"]
    assert manager.create_todo_list(
        '[{"id":"c","description":"third","dependencies":["missing"]}]'
    ).startswith("Error:")
    assert set(manager.tasks) == {"a", "b"}


def test_worker_output_fails_closed():
    from pydantic import ValidationError
    import pytest

    for value in (
        "",
        "I could not complete the task",
        '{"status":"cancelled"}',
        '{"status":"success","summary":"   "}',
    ):
        with pytest.raises(ValidationError):
            SubagentResult.model_validate_json(value)
    assert not SubagentResult(status="cancelled", summary="cancelled by user").success
    assert SubagentResult(status="success", summary="verified").success


async def test_loading_session_restores_completed_tasks_and_dependencies(
    tmp_path, monkeypatch
):
    from redlotus.tools.conversation_log import read_saved_model_messages_file

    system = configured_system(tmp_path, monkeypatch)
    system._task_manager.create_todo_list(
        '[{"id":"a","description":"done"},{"id":"b","description":"next","dependencies":["a"]}]'
    )
    system._task_manager.mark_task_complete("a", "verified artifact")
    system._task_manager.tasks["b"].status = TaskStatus.IN_PROGRESS

    async def create(*args, **kwargs):
        async def model(messages, info):
            yield "saved"

        return Agent(FunctionModel(stream_function=model))

    monkeypatch.setattr("redlotus.agent_core.system.create_coordinator_agent", create)
    await system.run_agent_system(UserMessage(text="保存当前状态"), ChatHistory())
    path = system._session_logs.for_agent("coordinator").model_messages_path()
    messages, meta = read_saved_model_messages_file(path)
    system._task_manager.reset()
    system.bind_loaded_snapshot("coordinator", path, meta)
    assert system._task_manager.tasks["a"].result == "verified artifact"
    assert system._task_manager.tasks["a"].status == TaskStatus.COMPLETED
    assert [task.id for task in system._task_manager.get_all_ready_tasks()] == ["b"]
    assert messages
    await system.shutdown()


async def test_rejected_first_request_keeps_trace_but_does_not_poison_next_turn(
    tmp_path, monkeypatch
):
    import pytest
    from pydantic_ai.capabilities import AbstractCapability
    from redlotus.ModelGateway.input_policy import InputLimitError

    system = configured_system(tmp_path, monkeypatch)
    seen = []

    class Budget(AbstractCapability):
        async def before_model_request(self, ctx, request_context):
            if any("OVERSIZED" in str(m.parts) for m in request_context.messages):
                raise InputLimitError("Reference exceeds request budget")
            return request_context

    async def create(*args, **kwargs):
        async def model(messages, info):
            seen.append(messages[-1].parts[0].content[0])
            yield "accepted"

        return Agent(FunctionModel(stream_function=model), capabilities=[Budget()])

    monkeypatch.setattr("redlotus.agent_core.system.create_coordinator_agent", create)
    history = ChatHistory()
    try:
        with pytest.raises(InputLimitError):
            await system.run_agent_system(UserMessage(text="OVERSIZED"), history)
        assert not history.messages
        await system.run_agent_system(
            UserMessage(text="smaller corrected input"), history
        )
        assert seen == ["smaller corrected input"]
        journals = list(system._memory.observations.root.parent.glob("*.jsonl"))
        assert any("OVERSIZED" in path.read_text(encoding="utf-8") for path in journals)
    finally:
        await system.shutdown()


async def test_project_switch_does_not_wait_for_scoped_memory_production(
    tmp_path, monkeypatch
):
    system = configured_system(tmp_path, monkeypatch)
    previous = system._memory
    from redlotus.agent_core.memory_service import MemoryService

    started, finish = threading.Event(), threading.Event()

    async def produce(producer, **kwargs):
        if producer.workspace.project_id == previous.workspace.project_id:
            started.set()
            await asyncio.to_thread(finish.wait)

    monkeypatch.setattr(
        previous,
        "schedule_processing",
        MemoryService.schedule_processing.__get__(previous),
    )
    monkeypatch.setattr(MemoryService, "process_pending", produce)
    target = tmp_path / "next-project"
    target.mkdir()
    switching = asyncio.create_task(system.switch_workspace(target))
    try:
        assert await asyncio.to_thread(started.wait, 3)
        await asyncio.sleep(0.05)
        assert switching.done(), "Directory switching is blocked by background memory"
        await switching
        assert system.workspace.root == target.resolve()
        assert previous.workspace.root == tmp_path.resolve()
        assert system._memory._perception_factory is previous._perception_factory
    finally:
        finish.set()
        await asyncio.gather(switching, return_exceptions=True)
        await system.shutdown()
        for memory in (previous, system._memory):
            assert (
                memory._background is None or not memory._background.thread.is_alive()
            )


async def test_exit_seals_unfinished_memory_without_starting_production(
    tmp_path, monkeypatch
):
    from redlotus.infra.persist_utils import read_locked_json
    from redlotus.runtime.subagents import SubagentSpec

    system = configured_system(tmp_path, monkeypatch)
    event = system._memory.observations.begin(
        "session", "turn", "unfinished project", []
    )
    event.status = "success"
    system._memory.observations.finish(event)
    started = threading.Event()

    async def pending():
        started.set()
        await asyncio.sleep(30)

    handle = system._memory_factory.start_background(
        SubagentSpec("memory", None, system.workspace, role="perception"), pending
    )
    assert await asyncio.to_thread(started.wait, 3)
    await asyncio.wait_for(system.shutdown(), 3)
    assert handle._future.cancelled() and not handle.thread.is_alive()
    jobs = list(system._memory.jobs_dir.glob("*.json"))
    assert len(jobs) == 1
    job = read_locked_json(jobs[0])
    assert job["window"]["new_turn_ids"] == [event.id]
    assert not job["done"] and job["result"] is None
    assert system._memory.observations.cursor() == 0
