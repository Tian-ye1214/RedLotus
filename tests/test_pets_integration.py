"""Shared command admission and application exit ownership."""
import asyncio
import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest


@pytest.mark.asyncio
async def test_shared_command_is_available_during_turn_and_completes_characters(monkeypatch):
    from redlotus.ui import cli_commands, widgets
    from redlotus.ui.console import AgentCliController

    assert "/pets" in AgentCliController.BUSY_SAFE_COMMANDS
    assert "/pets" in widgets.COMMAND_HELP
    assert widgets.completion_for_input("/pets ").choices == ("on", "off", "status")
    assert widgets.completion_for_input("/pets on i").choices == ("charcoal", "ivory")
    service = SimpleNamespace(command=AsyncMock(return_value=""))
    panels = []
    monkeypatch.setattr(cli_commands, "print_panel", lambda message, **kwargs: panels.append(message))
    controller = SimpleNamespace(system=SimpleNamespace(), pets=service)
    await cli_commands.SlashCommands(controller, None, "/pets on ivory").run()
    service.command.assert_awaited_once_with(["on", "ivory"])
    assert panels == []


@pytest.mark.asyncio
@pytest.mark.parametrize("surface", ["cli", "tui"])
@pytest.mark.parametrize("raw,response", [
    ("/pets", ""), ("/pets on ivory", ""), ("/pets off", ""),
    ("/pets status", "桌宠：运行中 · ivory · 172%"),
    ("/pets on ghost", "未知桌宠角色；可选角色：charcoal、ivory"),
    ("/pets on", 'Install desktop support with: pip install "RedLotus[pets]"'),
    ("/pets unknown", "用法：/pets [on [charcoal|ivory] | off | status]"),
])
async def test_pets_commands_use_plain_output_only_when_needed(monkeypatch, surface, raw, response):
    from rich.text import Text
    from redlotus.ui import cli_commands, presentation

    rendered = []
    if surface == "cli":
        sink = presentation.LegacyOutputSink(SimpleNamespace(print=rendered.append))
    else:
        app = SimpleNamespace(call_ui=lambda callback: callback())
        log = SimpleNamespace(write=lambda item, **kwargs: rendered.append(item))
        sink = presentation.TextualOutputSink(app, log)
    monkeypatch.setattr(presentation, "_sink", sink)
    service = SimpleNamespace(command=AsyncMock(return_value=response))
    controller = SimpleNamespace(system=SimpleNamespace(), pets=service)
    await cli_commands.SlashCommands(controller, None, raw).run()
    service.command.assert_awaited_once_with(raw.split(maxsplit=2)[1:])
    assert all(isinstance(item, Text) for item in rendered)
    assert [item.plain for item in rendered] == ([response] if response else [])


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [None, RuntimeError, asyncio.CancelledError])
async def test_common_exit_reaps_pet_before_other_services(isolated_config, monkeypatch, failure):
    from redlotus.api import base
    from redlotus.pets.factory import PetFactory
    from redlotus.TTS.service import SpeechService
    from redlotus.ui.console import AgentCliController

    events = []
    pet = SimpleNamespace(close=AsyncMock(side_effect=lambda: events.append("pets")))
    monkeypatch.setattr(PetFactory, "service", lambda: pet)
    monkeypatch.setattr(base, "install_stop_handlers", lambda event: None)
    monkeypatch.setattr(base, "start_speech", AsyncMock())
    monkeypatch.setattr(base, "close_all_clients", AsyncMock(side_effect=lambda: events.append("clients")))
    monkeypatch.setattr(SpeechService, "close_shared", AsyncMock(side_effect=lambda: events.append("speech")))

    async def interactive(controller, **kwargs):
        assert controller.pets is pet
        if failure:
            raise failure()

    monkeypatch.setattr(AgentCliController, "run_interactive", interactive)
    system = SimpleNamespace(shutdown=AsyncMock(side_effect=lambda: events.append("system")))
    if failure:
        with pytest.raises(failure):
            await base.run_cli(system)
    else:
        await base.run_cli(system)
    assert events == ["pets", "system", "speech", "clients"]


def test_frozen_child_dispatch_precedes_application_import(monkeypatch):
    root = Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location("pet_test_launcher", root / "main.py")
    launcher = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(launcher)
    calls = []
    entry = SimpleNamespace(run=lambda args: calls.append(args) or 0)
    monkeypatch.setitem(sys.modules, "redlotus.pets.desktop", SimpleNamespace(PetApplication=entry))
    monkeypatch.setitem(sys.modules, "redlotus.api.base", None)
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "argv", ["RedLotus.exe", "--pets-child", "ivory"])
    with pytest.raises(SystemExit) as stopped:
        launcher.main()
    assert stopped.value.code == 0
    assert calls == [["ivory"]]


def stream_delta(text, kind="text"):
    return SimpleNamespace(event_kind="part_delta", delta=SimpleNamespace(part_delta_kind=kind, content_delta=text))


@pytest.mark.asyncio
async def test_pet_gets_filtered_stream_and_final_text_without_voice_or_terminal_streaming():
    from redlotus.core.gateway import coordinator_stream_handler

    updates = []
    async def publish(**message): updates.append(message)
    state = SimpleNamespace(generation=(0, 0), turn_id="turn", voice_enabled=False, reply_output=publish)
    ui = SimpleNamespace(supports_model_stream=lambda: False, update_output=lambda *args: None)
    system = SimpleNamespace(session_key="session", _session=state, presentation=ui, workspace=None)
    handler = coordinator_stream_handler(system, display=False)
    async def events():
        yield stream_delta("hidden thinking", "thinking")
        yield stream_delta("第一段。<")
        yield stream_delta("!--REDLOTUS_GOAL:DONE-->第二段。")
        yield stream_delta("hidden tool", "tool_call")
    await handler(None, events())
    await handler.finish_reply("done", "最终正文")
    assert [row["phase"] for row in updates] == ["start", "delta", "delta", "done"]
    assert "".join(row["text"] for row in updates if row["phase"] == "delta") == "第一段。第二段。"
    assert updates[-1]["text"] == "最终正文"
    assert len({row["reply_id"] for row in updates}) == 1


@pytest.mark.asyncio
async def test_pet_body_survives_voice_failure_and_late_session_text_is_rejected():
    from redlotus.core.gateway import coordinator_stream_handler

    updates = []
    async def publish(**message): updates.append(message)
    def broken_voice(*args, **kwargs): raise RuntimeError("voice unavailable")
    state = SimpleNamespace(generation=(0, 0), turn_id="turn", voice_enabled=True,
                            begin_voice=broken_voice, reply_output=publish)
    ui = SimpleNamespace(supports_model_stream=lambda: False, update_output=lambda *args: None)
    system = SimpleNamespace(session_key="session", _session=state, presentation=ui, workspace=None)
    handler = coordinator_stream_handler(system)
    async def events():
        yield stream_delta("正文一")
        yield stream_delta("正文二")
        state.generation = (1, 0)
        yield stream_delta("旧会话迟到内容")
    await handler(None, events())
    await handler.finish_reply("done", "旧会话最终内容")
    assert "".join(row["text"] for row in updates) == "正文一正文二"


@pytest.mark.asyncio
@pytest.mark.parametrize("failure,phase", [(asyncio.CancelledError, "cancelled"), (RuntimeError, "failed")])
@pytest.mark.parametrize("tail", ["<", "<!", "<!-", "<!--REDLOTUS_GOAL:DONE"])
@pytest.mark.parametrize("prefix", ["", "partial"])
async def test_pet_stream_failure_preserves_partial_reply_once(failure, phase, tail, prefix):
    from redlotus.core.gateway import coordinator_stream_handler

    updates = []
    async def publish(**message): updates.append(message)
    state = SimpleNamespace(generation=(0, 0), turn_id="turn", voice_enabled=False, reply_output=publish)
    ui = SimpleNamespace(supports_model_stream=lambda: False, update_output=lambda *args: None)
    system = SimpleNamespace(session_key="session", _session=state, presentation=ui, workspace=None)
    handler = coordinator_stream_handler(system)
    async def events():
        yield stream_delta(prefix + tail)
        if phase == "cancelled": state.generation = (0, 1)
        raise failure()
    with pytest.raises(failure):
        await handler(None, events())
    await handler.finish_reply(phase, "")
    expected = prefix + ("" if tail.startswith("<!--") else tail)
    assert "".join(row["text"] for row in updates) == expected
    assert [row["phase"] for row in updates].count(phase) == bool(expected)


@pytest.mark.asyncio
async def test_broken_pet_callback_does_not_stop_model_or_voice():
    from redlotus.core.gateway import coordinator_stream_handler

    spoken, seen = [], []
    async def publish(**message):
        seen.append(message)
        raise RuntimeError("pet unavailable")
    class Voice:
        async def feed(self, text): spoken.append(text)
        async def finish(self): pass
    state = SimpleNamespace(generation=(0, 0), turn_id="turn", voice_enabled=True,
                            begin_voice=lambda *args, **kwargs: Voice(), reply_output=publish)
    ui = SimpleNamespace(supports_model_stream=lambda: False, update_output=lambda *args: None)
    system = SimpleNamespace(session_key="session", _session=state, presentation=ui, workspace=None)
    handler = coordinator_stream_handler(system)
    async def events():
        yield stream_delta("first")
        yield stream_delta("second")
    await handler(None, events())
    await handler.finish_reply("done", "firstsecond")
    assert spoken == ["first", "second"] and len(seen) == 1


@pytest.mark.asyncio
async def test_controller_binds_reply_callback_and_clears_before_session_reset(monkeypatch):
    from redlotus.pets.factory import PetFactory
    from redlotus.ui.console import AgentCliController

    events = []
    async def clear(): events.append("clear")
    pet = SimpleNamespace(publish_reply=AsyncMock(), clear_reply=clear)
    monkeypatch.setattr(PetFactory, "service", lambda: pet)
    state = SimpleNamespace(reply_output=None)
    system = SimpleNamespace(_session=state, _memory=SimpleNamespace(_processing=asyncio.Lock()),
                             reset_session=AsyncMock(side_effect=lambda: events.append("reset")))
    controller = AgentCliController(system)
    monkeypatch.setattr(controller, "_prepare_session_logs", lambda: None)
    assert controller.new_session_state() is state and state.reply_output == pet.publish_reply
    await controller.reset_session(SimpleNamespace(reset=lambda: events.append("history")))
    assert events == ["clear", "reset", "history"]


@pytest.mark.asyncio
async def test_completed_turn_keeps_pending_voice_valid_but_rejects_late_pet_text():
    from redlotus.core.gateway import coordinator_stream_handler

    updates, voice_guards = [], []
    async def publish(**message): updates.append(message)
    class Voice:
        async def feed(self, text): pass
        async def finish(self): pass
    def begin_voice(workspace, *, is_current):
        voice_guards.append(is_current)
        return Voice()
    state = SimpleNamespace(generation=(0, 0), turn_id="turn", voice_enabled=True,
                            begin_voice=begin_voice, reply_output=publish)
    ui = SimpleNamespace(supports_model_stream=lambda: False, update_output=lambda *args: None)
    system = SimpleNamespace(session_key="session", _session=state, presentation=ui, workspace=None)
    handler = coordinator_stream_handler(system)
    async def events(): yield stream_delta("正文")
    await handler(None, events())
    state.turn_id = None
    assert voice_guards[0](), "normal turn completion must allow queued voice to finish"
    await handler.finish_reply("done", "迟到正文")
    assert [row["phase"] for row in updates] == ["start", "delta"]
    state.generation = (1, 0)
    assert not voice_guards[0](), "session cancellation still invalidates voice"


@pytest.mark.asyncio
@pytest.mark.parametrize("switch", ["session", "turn"])
async def test_error_after_identity_change_does_not_flush_old_reply_to_callback(switch):
    from redlotus.core.gateway import coordinator_stream_handler

    updates = []
    async def publish(**message): updates.append(message)
    state = SimpleNamespace(generation=(0, 0), turn_id="old", voice_enabled=False, reply_output=publish)
    ui = SimpleNamespace(supports_model_stream=lambda: False, update_output=lambda *args: None)
    system = SimpleNamespace(session_key="session", _session=state, presentation=ui, workspace=None)
    handler = coordinator_stream_handler(system)
    async def events():
        yield stream_delta("old<")
        if switch == "session":
            system.session_key, state.generation = "new", (1, 0)
        else:
            state.turn_id = "new"
        raise RuntimeError("late error")
    with pytest.raises(RuntimeError):
        await handler(None, events())
    assert [row["phase"] for row in updates] == ["start", "delta"]
    assert "".join(row["text"] for row in updates) == "old"
