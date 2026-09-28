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
    service = SimpleNamespace(command=AsyncMock(return_value="桌宠：已关闭"))
    panels = []
    monkeypatch.setattr(cli_commands, "print_panel", lambda message, **kwargs: panels.append(message))
    controller = SimpleNamespace(system=SimpleNamespace(), pets=service)
    await cli_commands.SlashCommands(controller, None, "/pets on ivory").run()
    service.command.assert_awaited_once_with(["on", "ivory"])
    assert panels == ["桌宠：已关闭"]


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
