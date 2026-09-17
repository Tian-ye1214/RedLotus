"""Session-picker metadata and restored-conversation UI behavior."""

import asyncio
from datetime import datetime, timezone

import pytest
from pydantic_ai.messages import (
    ModelRequest,
    ModelResponse,
    TextPart,
    TextContent,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
)
from textual.widgets import Button, Static

from redlotus.core.cli_commands import WorkspaceSnapshot, list_workspace_snapshots
from redlotus.core.console import (
    SnapshotAction,
    SnapshotSelection,
    visible_conversation_entries,
)
from redlotus.core.presentation import set_output_sink
from redlotus.core.session import SessionFile
from redlotus.core.tui import AgentInput, RedLotusTui, SnapshotPickScreen
from redlotus.tools.interaction import UserMessage
from redlotus.tools.references import ReferenceFile, ReferencePart
from test_system import configured_system


async def _until(predicate):
    async with asyncio.timeout(5):
        while not predicate():
            await asyncio.sleep(0.01)


async def test_loading_locks_input_during_discovery_and_rejects_duplicate_picker(tmp_path, monkeypatch):
    import threading

    system = configured_system(tmp_path, monkeypatch)
    controller, started, release = system._cli_controller, threading.Event(), threading.Event()
    state = controller.new_session_state()
    scans = []

    def discover(**kwargs):
        scans.append(kwargs)
        started.set()
        release.wait(5)
        return []

    monkeypatch.setattr("redlotus.core.console.list_workspace_snapshots", discover)
    task = asyncio.create_task(controller.enter_current_workspace(state=state))
    try:
        await _until(started.is_set)
        assert controller.is_transitioning
        assert await controller.enter_current_workspace(state=state) is None
        assert len(scans) == 1
        release.set()
        await task
        assert not controller.is_transitioning
        assert system.session_key is None
    finally:
        release.set()
        await task
        await system.shutdown()


async def test_fresh_load_restores_project_picture_outside_process_workspace(tmp_path, monkeypatch):
    from pydantic_ai import BinaryContent
    from redlotus.core.agents import workspace_context

    project = tmp_path / "带图片的项目"
    project.mkdir()
    writer = configured_system(project, monkeypatch)
    original = b"immutable-picture-bytes"
    messages = [ModelRequest(parts=[UserPromptPart([
        "继续分析原图片", BinaryContent(data=original, media_type="image/png")
    ])])]
    with workspace_context(writer.workspace):
        await writer.bind_session("picture-session")
        writer._session_file.save_context(messages, turn_id="picture-turn")
        saved_path = writer._session_file.path
    await writer.shutdown()

    reader = configured_system(project, monkeypatch)
    state = reader.new_cli_session_state()

    async def picker(snapshots):
        chosen = next(item for item in snapshots if item.path == saved_path)
        return SnapshotSelection(SnapshotAction.RESTORE, chosen)

    reader._cli_controller.set_snapshot_picker(picker)
    try:
        assert await reader._cli_controller.enter_current_workspace(state=state, force_picker=True)
        assert reader.session_key == "picture-session"
        assert reader._session_file.path == saved_path
        assert state.history.messages[0].parts[0].content[1].data == original
        assert reader._session_file.read_turn("picture-turn")[0].parts[0].content[1].data == original
        assert not (tmp_path / "WorkDatabase" / "references").exists()
    finally:
        await reader.shutdown()


def _snapshot(path, *, identity="restored-id", error=""):
    return WorkspaceSnapshot(
        path,
        {
            "session_id": identity,
            "completed_turns": 3,
            "interrupted_turn": {"id": "turn-4"},
        },
        datetime(2026, 9, 17, 8, 30, tzinfo=timezone.utc),
        "coordinator",
        "2026-09-16",
        "部署进度",
        28,
        error,
    )


def test_snapshot_label_uses_title_local_activity_real_turns_and_status(tmp_path):
    snapshot = _snapshot(tmp_path / "restored" / "model_messages.json")

    assert "标题：部署进度" in snapshot.label
    assert "本地活动：" in snapshot.label
    assert "3 回合" in snapshot.label
    assert "状态：上次已中断" in snapshot.label
    assert "会话：restored-id" in snapshot.label


def test_picker_inventory_keeps_corrupt_session_visible_without_hiding_healthy_one(
    tmp_path,
):
    healthy = SessionFile.create(tmp_path, "project", session_id="healthy")
    broken_path = tmp_path / "broken" / "model_messages.json"
    broken_path.parent.mkdir()
    broken_path.write_text("not a session", encoding="utf-8")

    snapshots = list_workspace_snapshots(root=tmp_path, include_unloadable=True)

    assert healthy.path in [snapshot.path for snapshot in snapshots]
    broken = next(snapshot for snapshot in snapshots if snapshot.path == broken_path)
    assert not broken.is_loadable
    assert "状态：损坏" in broken.label


def test_visible_conversation_entries_keep_user_message_text_without_context_or_references(
    tmp_path,
):
    reference = ReferenceFile(
        id="ref-1",
        project_id="project",
        name="deploy-notes.txt",
        source="deploy-notes.txt",
        media_type="text/plain",
        byte_size=42,
        sha256="x" * 64,
        snapshot=tmp_path / "deploy-notes.txt",
        parts=[ReferencePart.from_text("REFERENCE BODY MUST NOT APPEAR")],
    )
    prompt = UserMessage(text="请检查部署", references=[reference]).to_prompt()
    messages = [
        ModelRequest(
            parts=[
                UserPromptPart(prompt),
                ToolReturnPart("shell", "TOOL BODY MUST NOT APPEAR", "call-1"),
            ]
        ),
        ModelRequest(
            parts=[
                UserPromptPart(
                    [
                        TextContent(
                            "CONTEXT SUMMARY MUST NOT APPEAR",
                            metadata={"origin": "context_summary"},
                        )
                    ]
                )
            ]
        ),
        ModelRequest(
            parts=[
                UserPromptPart(
                    [
                        TextContent(
                            "MEMORY CONTROL MUST NOT APPEAR",
                            metadata={"origin": "memory_control"},
                        )
                    ]
                )
            ]
        ),
        ModelResponse(
            parts=[
                ToolCallPart("shell", {"command": "secret"}, "call-1"),
                TextPart("部署检查完成"),
            ]
        ),
    ]

    entries = visible_conversation_entries(messages)

    assert [(entry.role, entry.text) for entry in entries] == [
        ("用户", "请检查部署"),
        ("助手", "部署检查完成"),
    ]
    hidden = (
        "TOOL BODY MUST NOT APPEAR",
        "REFERENCE BODY MUST NOT APPEAR",
        "Runtime context, generated by the application",
        "CONTEXT SUMMARY MUST NOT APPEAR",
        "MEMORY CONTROL MUST NOT APPEAR",
    )
    assert all(value not in entry.text for entry in entries for value in hidden)


async def test_controller_notifies_ui_after_restoring_repaired_conversation(
    tmp_path, monkeypatch
):
    system = configured_system(tmp_path, monkeypatch)
    controller = system._cli_controller
    state = controller.new_session_state()
    snapshot = _snapshot(tmp_path / "saved" / "model_messages.json")
    saved = [ModelRequest(parts=[UserPromptPart("原始提问")])]
    repaired = [
        ModelRequest(parts=[UserPromptPart("原始提问")]),
        ModelResponse(parts=[TextPart("恢复后的回答")]),
    ]
    shown = []

    async def picker(_snapshots):
        return SnapshotSelection(SnapshotAction.RESTORE, snapshot)

    async def bind(agent, path, meta):
        await system.bind_session(meta["session_id"])
        return repaired

    monkeypatch.setattr(
        "redlotus.core.console.list_workspace_snapshots", lambda **_: [snapshot]
    )
    monkeypatch.setattr(
        "redlotus.core.console.read_saved_model_messages_file",
        lambda _path, **_: (saved, {"session_id": "restored-id"}),
    )
    monkeypatch.setattr(system, "bind_loaded_snapshot", bind)
    controller.set_snapshot_picker(picker)
    controller.set_snapshot_loaded_callback(
        lambda chosen, messages: shown.append((chosen, messages))
    )
    try:
        assert await controller.enter_current_workspace(state=state, force_picker=True)
        assert shown == [(snapshot, repaired)]
    finally:
        await system.shutdown()


async def test_tui_keeps_session_load_control_visible_and_uses_controller_path(
    tmp_path, monkeypatch
):
    system = configured_system(tmp_path, monkeypatch)
    controller = system._cli_controller
    commands = []
    broken = _snapshot(
        tmp_path / "broken" / "model_messages.json", identity="broken-id", error="JSON 损坏"
    )
    healthy = _snapshot(tmp_path / "healthy" / "model_messages.json")

    monkeypatch.setattr(system, "prepare_cli_session", lambda: asyncio.sleep(0, result=()))
    monkeypatch.setattr(controller, "enter_current_workspace", lambda: asyncio.sleep(0))

    async def process_line(value, state, **kwargs):
        commands.append((value, state, kwargs))
        return "continue"

    monkeypatch.setattr(system, "process_cli_line", process_line)
    app = RedLotusTui(system)

    async def choose_healthy(pilot):
        task = asyncio.create_task(app.pick_snapshot([broken, healthy]))
        await pilot.pause()
        await pilot.press("down", "enter")
        return await asyncio.wait_for(task, 2)

    try:
        async with app.run_test(size=(100, 28)) as pilot:
            button = app.query_one("#session-load", Button)
            assert button.label.plain == "会话 / 加载"
            assert "项目：" in app.query_one("#session-context", Static).render().plain
            selected = await choose_healthy(pilot)
            assert selected.action is SnapshotAction.RESTORE
            assert selected.snapshot is healthy
            await pilot.click("#session-load")
            await _until(lambda: bool(commands))
        assert commands == [("/load", app.state, {"wait_for_turn": False, "goal_mode": False, "urgent": False})]
    finally:
        set_output_sink(None)
        await system.shutdown()


async def test_tui_locks_input_immediately_while_startup_picker_is_pending(
    tmp_path, monkeypatch
):
    """A startup restore choice must block input before the next UI timer tick."""
    from redlotus.core.config import session_data_dir

    system = configured_system(tmp_path, monkeypatch)
    controller = system._cli_controller
    saved = SessionFile.create(
        session_data_dir(system.workspace),
        system.workspace.project_id,
        session_id="saved",
        workspace=system.workspace,
    )
    monkeypatch.setattr(
        system, "prepare_cli_session", lambda: asyncio.sleep(0, result=())
    )
    app = RedLotusTui(system)
    try:
        async with app.run_test(size=(100, 28)) as pilot:
            await _until(lambda: isinstance(app.screen, SnapshotPickScreen))
            assert controller.is_transitioning
            assert app.query_one("#input", AgentInput).disabled
            assert app.query_one("#session-load", Button).disabled
            await pilot.press("enter")
            await _until(lambda: not controller.is_transitioning)
            assert system.session_key is None
            assert app.state.is_first_input
            saved_paths = [
                entry.path
                for entry in SessionFile.scan_info(session_data_dir(system.workspace))
            ]
            assert saved_paths == [saved.path]
            await pilot.click("#session-load")
            await _until(lambda: isinstance(app.screen, SnapshotPickScreen))
            await pilot.press("down", "enter")
            await _until(lambda: system.session_key == "saved")
            assert not app.state.is_first_input
    finally:
        set_output_sink(None)
        await system.shutdown()


async def test_cancelling_startup_entry_dismisses_its_picker(tmp_path, monkeypatch):
    """Cancelling the startup worker must not leave a modal or admission lock behind."""
    from redlotus.core.config import session_data_dir

    system = configured_system(tmp_path, monkeypatch)
    controller = system._cli_controller
    SessionFile.create(
        session_data_dir(system.workspace),
        system.workspace.project_id,
        session_id="saved",
        workspace=system.workspace,
    )
    monkeypatch.setattr(
        system, "prepare_cli_session", lambda: asyncio.sleep(0, result=())
    )
    app = RedLotusTui(system)
    monkeypatch.setattr(app, "_schedule_workspace_enter", lambda: None)
    try:
        async with app.run_test(size=(100, 28)):
            entering = asyncio.create_task(app._enter_workspace_after_mount())
            await _until(lambda: isinstance(app.screen, SnapshotPickScreen))
            entering.cancel()
            with pytest.raises(asyncio.CancelledError):
                await entering
            await _until(lambda: not isinstance(app.screen, SnapshotPickScreen))
            assert not controller.is_transitioning
    finally:
        set_output_sink(None)
        await system.shutdown()


async def test_textual_picker_selects_new_restore_and_cancels_late_result(
    tmp_path, monkeypatch
):
    system = configured_system(tmp_path, monkeypatch)
    controller = system._cli_controller
    snapshot = _snapshot(tmp_path / "saved" / "model_messages.json")

    monkeypatch.setattr(
        system, "prepare_cli_session", lambda: asyncio.sleep(0, result=())
    )
    monkeypatch.setattr(controller, "enter_current_workspace", lambda: asyncio.sleep(0))
    app = RedLotusTui(system)

    async def choose(pilot, *keys):
        task = asyncio.create_task(app.pick_snapshot([snapshot]))
        await _until(lambda: isinstance(app.screen, SnapshotPickScreen))
        await pilot.press(*keys)
        return await asyncio.wait_for(task, 2)

    try:
        async with app.run_test(size=(100, 28)) as pilot:
            assert (await choose(pilot, "escape")).action is SnapshotAction.CANCEL
            assert (await choose(pilot, "enter")).action is SnapshotAction.NEW
            assert (await choose(pilot, "down", "enter")) == SnapshotSelection(
                SnapshotAction.RESTORE, snapshot
            )

            late = asyncio.create_task(app.pick_snapshot([snapshot]))
            await _until(lambda: isinstance(app.screen, SnapshotPickScreen))
            system._session.reset(discard=True)
            await pilot.press("down", "enter")
            assert (await asyncio.wait_for(late, 2)).action is SnapshotAction.CANCEL
    finally:
        set_output_sink(None)
        await system.shutdown()


async def test_load_command_does_not_bypass_an_active_turn(tmp_path, monkeypatch):
    system = configured_system(tmp_path, monkeypatch)
    controller = system._cli_controller
    state = controller.new_session_state()
    entered = False

    async def unexpected_picker(*_args, **_kwargs):
        nonlocal entered
        entered = True
        return None

    monkeypatch.setattr(controller, "enter_current_workspace", unexpected_picker)
    system._session.active = True
    try:
        await controller.process_line("/load", state, wait_for_turn=False)
        assert not entered
    finally:
        system._session.active = False
        await system.shutdown()
