"""The acceptance driver must correlate turns even when clear runs concurrently."""

import asyncio
from pathlib import Path

import pytest
from pydantic_ai import Agent
from pydantic_ai.models.function import FunctionModel

from test_entries import configure_cli_hooks
from test_system import configured_system


@pytest.mark.parametrize("control", ["/clear", "/stop"])
async def test_driver_never_assigns_new_reply_to_a_cancelled_input(
    tmp_path, monkeypatch, control
):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / "scripts"))
    from real_acceptance import ApplicationDriver

    system = configured_system(tmp_path, monkeypatch)
    configure_cli_hooks(system, monkeypatch)
    monkeypatch.setattr(
        "redlotus.agent_core.system.AgentSystem", lambda **kwargs: system
    )
    monkeypatch.setattr("redlotus.workspace.workspace.set_workspace", lambda path: None)
    monkeypatch.setattr(
        system,
        "generate_task_title",
        lambda text: asyncio.sleep(0, result="driver-check"),
    )
    driver = ApplicationDriver(tmp_path, tmp_path / "driver-records")
    driver.state.is_first_input = False
    started = asyncio.Event()

    async def create(*args, **kwargs):
        async def model(messages, info):
            text = messages[-1].parts[0].content[0]
            if text == "old input":
                started.set()
                await asyncio.Event().wait()
            yield "NEW_REPLY"

        return Agent(FunctionModel(stream_function=model))

    monkeypatch.setattr("redlotus.agent_core.system.create_coordinator_agent", create)
    try:
        async with asyncio.timeout(10):
            old = asyncio.create_task(driver.say("old input"))
            await started.wait()
            await system.process_cli_line(control, driver.state, wait_for_turn=False)
            await driver.say("new input")
            await old
        records = {record["input"]: record for record in driver.outputs}
        assert records["old input"]["output"] == ""
        assert records["old input"]["status"] == "cancelled"
        assert records["new input"]["output"] == "NEW_REPLY"
        assert records["old input"]["input_id"] != records["new input"]["input_id"]
    finally:
        await system.shutdown()
