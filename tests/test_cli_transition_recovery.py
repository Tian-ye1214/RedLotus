"""CLI/TUI recovery behavior at workspace and session boundaries."""

import asyncio
import inspect
import threading
from datetime import datetime, timezone

import pytest
from textual.widgets import Input

from redlotus.core.presentation import set_output_sink
from redlotus.core.cli_commands import WorkspaceSnapshot
from redlotus.core.tui import AgentInput, RedLotusTui
from test_system import configured_system


async def _until(predicate):
    async with asyncio.timeout(5):
        while not predicate():
            await asyncio.sleep(0.01)


def _allow_cli_input(monkeypatch):
    monkeypatch.setattr(
        "redlotus.core.console.app_config.missing_main_api_keys", lambda: ()
    )
    monkeypatch.setattr("redlotus.core.console.app_config.reload_config", lambda: None)


async def test_transition_input_is_not_admitted_while_workspace_switches(
    tmp_path, monkeypatch
):
    system = configured_system(tmp_path, monkeypatch)
    cli = system._cli_controller
    state = cli.new_session_state()
    state.is_first_input = False
    target = tmp_path / "next-project"
    target.mkdir()
    entered, release = asyncio.Event(), asyncio.Event()
    parsed, started = [], []
    original_switch = system.switch_workspace

    async def delayed_switch(path):
        entered.set()
        await release.wait()
        await original_switch(path)

    async def parse(raw_input, *, workspace):
        parsed.append((raw_input, workspace.root))
        return []

    async def start(raw_input, _state, *, admission, references, **_):
        started.append((raw_input, admission.workspace.root, await references))

    monkeypatch.setattr(system, "switch_workspace", delayed_switch)
    monkeypatch.setattr("redlotus.core.console.load_file_refs", parse)
    monkeypatch.setattr(cli, "_start_user_turn_from_raw_input", start)
    _allow_cli_input(monkeypatch)
    transition = asyncio.create_task(cli.reset_session(state.history, workspace=target))
    submitted = None
    try:
        await asyncio.wait_for(entered.wait(), 5)
        submitted = asyncio.create_task(
            cli.process_line("typed during switch", state, wait_for_turn=False)
        )
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert parsed == []
        release.set()
        await transition
        await submitted
        await system._session.queue.join()
        assert parsed == []
        assert started == []
    finally:
        release.set()
        await asyncio.gather(
            transition, *(task for task in (submitted,) if task), return_exceptions=True
        )
        await system.shutdown()


async def test_pretransition_admission_is_invalidated_before_its_turn_starts(
    tmp_path, monkeypatch
):
    system = configured_system(tmp_path, monkeypatch)
    cli = system._cli_controller
    state = cli.new_session_state()
    state.is_first_input = False
    parsing_started, release = asyncio.Event(), asyncio.Event()
    turns = []

    async def parse(*_args, **_kwargs):
        parsing_started.set()
        await release.wait()
        return []

    def start_turn(*args, **_kwargs):
        turns.append(args)
        return asyncio.create_task(asyncio.sleep(0))

    monkeypatch.setattr("redlotus.core.console.load_file_refs", parse)
    monkeypatch.setattr(system, "_start_user_turn", start_turn)
    monkeypatch.setattr(cli, "_publish_context_usage", lambda *_: asyncio.sleep(0))
    _allow_cli_input(monkeypatch)
    try:
        await cli.process_line("old workspace input", state, wait_for_turn=False)
        await asyncio.wait_for(parsing_started.wait(), 5)
        await cli.reset_session(state.history)
        release.set()
        await system._session.queue.join()
        assert turns == []
    finally:
        release.set()
        await system.shutdown()


async def test_failed_transition_keeps_original_session_without_old_workspace_work(
    tmp_path, monkeypatch
):
    from pydantic_ai.messages import ModelRequest, UserPromptPart

    system = configured_system(tmp_path, monkeypatch)
    cli = system._cli_controller
    state = cli.new_session_state()
    state.is_first_input = False
    previous = ModelRequest(parts=[UserPromptPart("current session")])
    state.history.set_messages([previous])
    await system.bind_session("current-id")
    previous_workspace = system.workspace
    target = tmp_path / "unavailable-project"
    entered, release = asyncio.Event(), asyncio.Event()
    parsed, started = [], []

    async def failed_switch(_path):
        entered.set()
        await release.wait()
        raise OSError("workspace unavailable")

    async def parse(raw_input, *, workspace):
        parsed.append((raw_input, workspace.root))
        return []

    async def start(raw_input, *_args, **_kwargs):
        started.append(raw_input)

    monkeypatch.setattr(system, "switch_workspace", failed_switch)
    monkeypatch.setattr("redlotus.core.console.load_file_refs", parse)
    monkeypatch.setattr(cli, "_start_user_turn_from_raw_input", start)
    _allow_cli_input(monkeypatch)
    transition = asyncio.create_task(cli.reset_session(state.history, workspace=target))
    submitted = None
    try:
        await asyncio.wait_for(entered.wait(), 5)
        submitted = asyncio.create_task(
            cli.process_line("keep this editable", state, wait_for_turn=False)
        )
        release.set()
        with pytest.raises(OSError, match="workspace unavailable"):
            await transition
        await submitted
        assert parsed == []
        assert started == []
        assert cli.last_rejected_input is None
        assert system.session_key == "current-id"
        assert system.workspace is previous_workspace
        assert state.history.messages == [previous]
    finally:
        release.set()
        await asyncio.gather(transition, *(task for task in (submitted,) if task), return_exceptions=True)
        await system.shutdown()


async def test_textual_workspace_switch_locks_input_and_keeps_draft_on_failure(
    tmp_path, monkeypatch
):
    from pydantic_ai.messages import ModelRequest, UserPromptPart

    system = configured_system(tmp_path, monkeypatch)
    cli = system._cli_controller
    previous = ModelRequest(parts=[UserPromptPart("current session")])
    previous_workspace = system.workspace
    target = tmp_path / "unavailable-project"
    target.mkdir()
    entered, release = asyncio.Event(), asyncio.Event()
    processed = []

    async def failed_switch(_path):
        entered.set()
        await release.wait()
        raise OSError("workspace unavailable")

    async def process_line(value, *_args, **_kwargs):
        processed.append(value)
        return "continue"

    monkeypatch.setattr(system, "prepare_cli_session", lambda: asyncio.sleep(0, result=()))
    monkeypatch.setattr(cli, "enter_current_workspace", lambda: asyncio.sleep(0))
    monkeypatch.setattr(system, "switch_workspace", failed_switch)
    monkeypatch.setattr(system, "process_cli_line", process_line)
    await system.bind_session("current-id")
    app = RedLotusTui(system)
    transition = None
    try:
        async with app.run_test(size=(100, 28)) as pilot:
            await pilot.pause()
            app.state.is_first_input = False
            app.state.history.set_messages([previous])
            input_box = app.query_one("#input", AgentInput)
            input_box.value = "draft remains"
            transition = asyncio.create_task(
                cli.reset_session(app.state.history, workspace=target)
            )
            await asyncio.wait_for(entered.wait(), 5)
            app.refresh_status()
            assert input_box.disabled
            await app.action_submit_urgent()
            assert input_box.value == "draft remains"
            assert processed == []

            release.set()
            with pytest.raises(OSError, match="workspace unavailable"):
                await transition
            app.refresh_status()
            assert not input_box.disabled
            assert input_box.value == "draft remains"
        assert system.session_key == "current-id"
        assert system.workspace is previous_workspace
        assert app.state.history.messages == [previous]
    finally:
        release.set()
        await asyncio.gather(
            *(task for task in (transition,) if task), return_exceptions=True
        )
        set_output_sink(None)
        await system.shutdown()


async def test_slow_attachment_does_not_reorder_admitted_inputs(tmp_path, monkeypatch):
    system = configured_system(tmp_path, monkeypatch)
    cli = system._cli_controller
    state = cli.new_session_state()
    state.is_first_input = False
    parsing_started, release = asyncio.Event(), asyncio.Event()
    consumed = []

    async def parse(raw_input, *, workspace):
        if raw_input == "slow attachment":
            parsing_started.set()
            await release.wait()
        return [raw_input]

    async def start(raw_input, _state, *, references, **_):
        consumed.append((raw_input, await references))

    monkeypatch.setattr("redlotus.core.console.load_file_refs", parse)
    monkeypatch.setattr(cli, "_start_user_turn_from_raw_input", start)
    _allow_cli_input(monkeypatch)
    try:
        await cli.process_line("slow attachment", state, wait_for_turn=False)
        await asyncio.wait_for(parsing_started.wait(), 5)
        await cli.process_line("fast attachment", state, wait_for_turn=False)
        await asyncio.sleep(0)
        assert consumed == []
        release.set()
        await system._session.queue.join()
        assert consumed == [
            ("slow attachment", ["slow attachment"]),
            ("fast attachment", ["fast attachment"]),
        ]
    finally:
        release.set()
        await system.shutdown()


async def test_normal_input_signals_paused_save_before_admission_but_urgent_does_not(
    tmp_path, monkeypatch
):
    system = configured_system(tmp_path, monkeypatch)
    cli = system._cli_controller
    state = cli.new_session_state()
    state.is_first_input = False
    events = []
    original_admit = system._session.admit

    async def retry_saved_state():
        events.append("retry")

    def admit(*args, **kwargs):
        events.append("admit")
        return original_admit(*args, **kwargs)

    async def parse(*_args, **_kwargs):
        return []

    async def start(raw_input, *_args, **_kwargs):
        events.append(raw_input)

    monkeypatch.setattr(system, "retry_saved_state", retry_saved_state, raising=False)
    monkeypatch.setattr(system._session, "admit", admit)
    monkeypatch.setattr("redlotus.core.console.load_file_refs", parse)
    monkeypatch.setattr(cli, "_start_user_turn_from_raw_input", start)
    _allow_cli_input(monkeypatch)
    try:
        await cli.process_line("ordinary", state, wait_for_turn=False)
        await cli.process_line("urgent", state, wait_for_turn=False, urgent=True)
        await system._session.queue.join()
        assert events == ["retry", "admit", "admit", "ordinary", "urgent"]
    finally:
        await system.shutdown()


async def test_load_cancels_manual_compression_but_not_an_agent_turn(tmp_path, monkeypatch):
    system = configured_system(tmp_path, monkeypatch)
    cli = system._cli_controller
    state = cli.new_session_state()
    compression_started, compression_release, compression_cancelled = (
        asyncio.Event(),
        asyncio.Event(),
        asyncio.Event(),
    )
    ordinary_started, ordinary_release = asyncio.Event(), asyncio.Event()
    panels, picker_calls = [], []

    async def delayed_compression(_history):
        compression_started.set()
        try:
            await compression_release.wait()
        except asyncio.CancelledError:
            compression_cancelled.set()
            raise

    async def ordinary_agent_work():
        ordinary_started.set()
        await ordinary_release.wait()

    async def picker(**_kwargs):
        picker_calls.append("picker")

    async def sync_skills():
        pass

    monkeypatch.setattr(system, "_compress_context", delayed_compression)
    monkeypatch.setattr(system, "_sync_skills_for_user_turn", sync_skills)
    monkeypatch.setattr(cli, "enter_current_workspace", picker)
    monkeypatch.setattr(
        "redlotus.core.cli_commands.print_panel",
        lambda text, *, title: panels.append((title, text)),
    )
    compression = asyncio.create_task(system.compress_context(state.history))
    ordinary = None
    try:
        await asyncio.wait_for(compression_started.wait(), 5)
        assert system.has_current_turn
        assert system.is_compressing

        await cli._handle_slash_command("/compress", state)
        assert panels == [("上下文压缩", "上下文压缩正在处理中。")]

        await cli._handle_slash_command("/load", state)
        await asyncio.wait_for(compression_cancelled.wait(), 5)
        await compression
        assert picker_calls == ["picker"]
        assert not system.is_compressing

        ordinary = system._session.queue.submit(ordinary_agent_work)
        await asyncio.wait_for(ordinary_started.wait(), 5)
        assert system.has_current_turn
        await cli._handle_slash_command("/load", state)
        assert picker_calls == ["picker"]
    finally:
        compression_release.set()
        ordinary_release.set()
        await asyncio.gather(
            *(task for task in (compression, ordinary) if task),
            return_exceptions=True,
        )
        await system.shutdown()


async def test_first_input_saves_title_through_durable_write_boundary(
    tmp_path, monkeypatch
):
    system = configured_system(tmp_path, monkeypatch)
    cli = system._cli_controller
    state = cli.new_session_state()
    durable_metadata = []

    async def parse(*_args, **_kwargs):
        return []

    async def title(_text):
        return "durable title"

    async def durable_write(operation):
        result = operation()
        if inspect.isawaitable(result):
            result = await result
        if system._session_file is not None:
            durable_metadata.append(dict(system._session_file.metadata))
        return result

    def start_turn(*_args, **_kwargs):
        return asyncio.create_task(asyncio.sleep(0))

    monkeypatch.setattr("redlotus.core.console.load_file_refs", parse)
    monkeypatch.setattr(system, "generate_task_title", title)
    monkeypatch.setattr(system, "_durable_write", durable_write)
    monkeypatch.setattr(system, "_start_user_turn", start_turn)
    monkeypatch.setattr(cli, "_publish_context_usage", lambda *_: asyncio.sleep(0))
    _allow_cli_input(monkeypatch)
    try:
        await cli.process_line("first input", state, wait_for_turn=True)
        assert any(
            metadata.get("title") == "durable title"
            for metadata in durable_metadata
        )
        assert system._session_file.metadata["title"] == "durable title"
    finally:
        await system.shutdown()


async def test_load_read_failure_keeps_current_session(tmp_path, monkeypatch):
    from pydantic_ai.messages import ModelRequest, UserPromptPart

    from redlotus.core.console import SnapshotAction, SnapshotSelection

    system = configured_system(tmp_path, monkeypatch)
    cli = system._cli_controller
    state = cli.new_session_state()
    current = ModelRequest(parts=[UserPromptPart("current context")])
    snapshot = WorkspaceSnapshot(
        tmp_path / "saved" / "model_messages.json",
        {"session_id": "restored-id"},
        datetime.now(timezone.utc),
        "coordinator",
        "2026-09-16",
        "saved task",
        1,
    )

    async def picker(_snapshots):
        return SnapshotSelection(SnapshotAction.RESTORE, snapshot)

    def unreadable(_path):
        raise OSError("snapshot unreadable")

    monkeypatch.setattr(
        "redlotus.core.console.list_workspace_snapshots", lambda **_: [snapshot]
    )
    monkeypatch.setattr(
        "redlotus.core.console.read_saved_model_messages_file", unreadable
    )
    cli.set_snapshot_picker(picker)
    try:
        await system.bind_session("current-id")
        state.history.set_messages([current])
        assert await cli.enter_current_workspace(state=state, force_picker=True) is None
        assert system.session_key == "current-id"
        assert state.history.messages == [current]
    finally:
        await system.shutdown()


async def test_load_does_not_admit_input_during_snapshot_binding(
    tmp_path, monkeypatch
):
    from pydantic_ai.messages import ModelRequest, UserPromptPart

    from redlotus.core.console import SnapshotAction, SnapshotSelection

    system = configured_system(tmp_path, monkeypatch)
    cli = system._cli_controller
    state = cli.new_session_state()
    state.is_first_input = False
    saved = ModelRequest(parts=[UserPromptPart("saved context")])
    repaired = ModelRequest(parts=[UserPromptPart("repaired context")])
    snapshot = WorkspaceSnapshot(
        tmp_path / "saved" / "model_messages.json",
        {"session_id": "restored-id"},
        datetime.now(timezone.utc),
        "coordinator",
        "2026-09-16",
        "saved task",
        1,
    )
    bound, release = asyncio.Event(), asyncio.Event()
    admitted, parsed, started = [], [], []
    original_admit = system._session.admit

    async def picker(_snapshots):
        return SnapshotSelection(SnapshotAction.RESTORE, snapshot)

    async def bind(agent, path, meta):
        bound.set()
        await release.wait()
        await system.bind_session(meta["session_id"])
        return [repaired]

    def admit(workspace, **kwargs):
        admitted.append((workspace.root, system.session_key))
        return original_admit(workspace, **kwargs)

    async def parse(raw_input, *, workspace):
        parsed.append((raw_input, workspace.root, system.session_key))
        return []

    async def start(raw_input, _state, *, admission, references, **_):
        started.append(
            (raw_input, admission.workspace.root, system.session_key, await references)
        )

    monkeypatch.setattr(
        "redlotus.core.console.list_workspace_snapshots", lambda **_: [snapshot]
    )
    monkeypatch.setattr(
        "redlotus.core.console.read_saved_model_messages_file",
        lambda _path: ([saved], {"session_id": "restored-id"}),
    )
    monkeypatch.setattr(system, "bind_loaded_snapshot", bind)
    monkeypatch.setattr(system._session, "admit", admit)
    monkeypatch.setattr("redlotus.core.console.load_file_refs", parse)
    monkeypatch.setattr(cli, "_start_user_turn_from_raw_input", start)
    _allow_cli_input(monkeypatch)
    cli.set_snapshot_picker(picker)
    load = late_input = None
    try:
        await system.bind_session("current-id")
        load = asyncio.create_task(
            cli.enter_current_workspace(state=state, force_picker=True)
        )
        await asyncio.wait_for(bound.wait(), 5)
        late_input = asyncio.create_task(
            cli.process_line("late input", state, wait_for_turn=False)
        )
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert admitted == []
        assert parsed == []
        assert started == []

        release.set()
        assert await load is True
        await late_input
        await system._session.queue.join()
        assert admitted == []
        assert parsed == []
        assert started == []
        assert state.history.messages == [repaired]
    finally:
        release.set()
        await asyncio.gather(
            *(task for task in (load, late_input) if task), return_exceptions=True
        )
        await system.shutdown()


async def test_load_does_not_admit_input_during_snapshot_read(
    tmp_path, monkeypatch
):
    from pydantic_ai.messages import ModelRequest, UserPromptPart

    from redlotus.core.console import SnapshotAction, SnapshotSelection

    system = configured_system(tmp_path, monkeypatch)
    cli = system._cli_controller
    state = cli.new_session_state()
    state.is_first_input = False
    saved = ModelRequest(parts=[UserPromptPart("saved context")])
    repaired = ModelRequest(parts=[UserPromptPart("repaired context")])
    snapshot = WorkspaceSnapshot(
        tmp_path / "saved" / "model_messages.json",
        {"session_id": "restored-id"},
        datetime.now(timezone.utc),
        "coordinator",
        "2026-09-16",
        "saved task",
        1,
    )
    read_started, read_release = threading.Event(), threading.Event()
    admitted, parsed, started = [], [], []
    original_admit = system._session.admit

    async def picker(_snapshots):
        return SnapshotSelection(SnapshotAction.RESTORE, snapshot)

    def slow_read(_path):
        read_started.set()
        assert read_release.wait(5)
        return [saved], {"session_id": "restored-id"}

    async def bind(agent, path, meta):
        await system.bind_session(meta["session_id"])
        return [repaired]

    def admit(workspace, **kwargs):
        admitted.append((workspace.root, system.session_key))
        return original_admit(workspace, **kwargs)

    async def parse(raw_input, *, workspace):
        parsed.append((raw_input, workspace.root, system.session_key))
        return []

    async def start(raw_input, _state, *, admission, references, **_):
        started.append(
            (raw_input, admission.workspace.root, system.session_key, await references)
        )

    monkeypatch.setattr(
        "redlotus.core.console.list_workspace_snapshots", lambda **_: [snapshot]
    )
    monkeypatch.setattr("redlotus.core.console.read_saved_model_messages_file", slow_read)
    monkeypatch.setattr(system, "bind_loaded_snapshot", bind)
    monkeypatch.setattr(system._session, "admit", admit)
    monkeypatch.setattr("redlotus.core.console.load_file_refs", parse)
    monkeypatch.setattr(cli, "_start_user_turn_from_raw_input", start)
    _allow_cli_input(monkeypatch)
    cli.set_snapshot_picker(picker)
    load = late_input = None
    try:
        await system.bind_session("current-id")
        load = asyncio.create_task(
            cli.enter_current_workspace(state=state, force_picker=True)
        )
        await asyncio.wait_for(asyncio.to_thread(read_started.wait), 5)
        late_input = asyncio.create_task(
            cli.process_line("late input", state, wait_for_turn=False)
        )
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert admitted == []
        assert parsed == []
        assert started == []

        read_release.set()
        assert await load is True
        await late_input
        await system._session.queue.join()
        assert admitted == []
        assert parsed == []
        assert started == []
        assert state.history.messages == [repaired]
    finally:
        read_release.set()
        await asyncio.gather(
            *(task for task in (load, late_input) if task), return_exceptions=True
        )
        await system.shutdown()


async def test_textual_picker_exposes_new_restore_and_cancel(tmp_path, monkeypatch):
    system = configured_system(tmp_path, monkeypatch)
    cli = system._cli_controller
    monkeypatch.setattr(system, "prepare_cli_session", lambda: asyncio.sleep(0, result=()))
    monkeypatch.setattr(cli, "enter_current_workspace", lambda: asyncio.sleep(0))
    snapshot = WorkspaceSnapshot(
        tmp_path / "saved" / "model_messages.json",
        {"session_id": "restored-id"},
        datetime.now(timezone.utc),
        "coordinator",
        "2026-09-16",
        "saved task",
        1,
    )
    app = RedLotusTui(system)

    async def choose(pilot, *keys):
        task = asyncio.create_task(app.pick_snapshot([snapshot]))
        await pilot.pause()
        for key in keys:
            await pilot.press(key)
        return await asyncio.wait_for(task, 2)

    try:
        async with app.run_test(size=(100, 28)) as pilot:
            new = await choose(pilot, "enter")
            restored = await choose(pilot, "down", "enter")
            cancelled = await choose(pilot, "end", "enter")
        assert new.action.value == "new"
        assert restored.action.value == "restore"
        assert restored.snapshot is snapshot
        assert cancelled.action.value == "cancel"
    finally:
        set_output_sink(None)
        await system.shutdown()


async def test_picker_action_routes_new_restore_and_cancel_through_controller(
    tmp_path, monkeypatch
):
    from pydantic_ai.messages import ModelRequest, UserPromptPart

    from redlotus.core.console import SnapshotAction, SnapshotSelection

    system = configured_system(tmp_path, monkeypatch)
    cli = system._cli_controller
    state = cli.new_session_state()
    saved = ModelRequest(parts=[UserPromptPart("saved context")])
    repaired = ModelRequest(parts=[UserPromptPart("repaired context")])
    snapshot = WorkspaceSnapshot(
        tmp_path / "saved" / "model_messages.json",
        {"session_id": "restored-id"},
        datetime.now(timezone.utc),
        "coordinator",
        "2026-09-16",
        "saved task",
        1,
    )
    selection = SnapshotSelection(SnapshotAction.CANCEL)
    reset_calls, bound = [], []

    async def picker(_snapshots):
        return selection

    async def reset(history, **kwargs):
        reset_calls.append((history, kwargs))
        if prepare := kwargs.get("prepare"):
            assert await prepare()
        if restore := kwargs.get("restore"):
            await restore()
        else:
            history.reset()
        return True

    async def bind(agent, path, meta):
        bound.append((agent, path, meta))
        await system.bind_session(meta["session_id"])
        return [repaired]

    monkeypatch.setattr("redlotus.core.console.list_workspace_snapshots", lambda **_: [snapshot])
    monkeypatch.setattr(
        "redlotus.core.console.read_saved_model_messages_file",
        lambda _path: ([saved], {"session_id": "restored-id"}),
    )
    monkeypatch.setattr(cli, "reset_session", reset)
    monkeypatch.setattr(system, "bind_loaded_snapshot", bind)
    cli.set_snapshot_picker(picker)
    try:
        state.history.set_messages([saved])
        await system.bind_session("current-id")
        assert await cli.enter_current_workspace(state=state, force_picker=True) is None
        assert reset_calls == []
        assert system.session_key == "current-id"
        assert state.history.messages == [saved]

        selection = SnapshotSelection(SnapshotAction.NEW)
        assert await cli.enter_current_workspace(state=state, force_picker=True) is False
        assert reset_calls == [(state.history, {})]
        assert state.is_first_input

        selection = SnapshotSelection(SnapshotAction.RESTORE, snapshot)
        assert await cli.enter_current_workspace(state=state, force_picker=True) is True
        assert len(reset_calls) == 2
        assert reset_calls[1][0] is state.history
        assert set(reset_calls[1][1]) == {"prepare", "restore"}
        assert bound == [("coordinator", snapshot.path, {"session_id": "restored-id"})]
        assert system.session_key == "restored-id"
        assert state.history.messages == [repaired]
    finally:
        await system.shutdown()
