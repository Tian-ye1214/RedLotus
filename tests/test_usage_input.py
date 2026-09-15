"""Regression cases from the real usage audit; no network model requests."""

import asyncio

import pytest
from pydantic_ai import Agent
from pydantic_ai.messages import UserPromptPart
from pydantic_ai.models.function import FunctionModel

from redlotus.runtime.context import WorkspaceContext
from redlotus.tools.BasicTools import BasicToolkit
from test_entries import configure_cli_hooks
from test_system import configured_system


async def test_expired_urgent_does_not_capture_attachments_after_slow_parse(
    tmp_path, monkeypatch
):
    import io
    from PIL import Image
    from pydantic_ai.messages import BinaryContent
    from redlotus.agent_core.input_messages import UserMessage

    system = configured_system(tmp_path, monkeypatch)
    parsing, release = asyncio.Event(), asyncio.Event()
    picture = io.BytesIO()
    Image.new("RGB", (2, 2), "blue").save(picture, format="PNG")
    store = system._toolkit._references

    async def parse():
        parsing.set()
        try:
            await release.wait()
        except asyncio.CancelledError:
            await release.wait()
        return []

    try:
        async with system._session.turn("old task"):
            message = UserMessage(
                text="old",
                attachments=[
                    BinaryContent(data=picture.getvalue(), media_type="image/png")
                ],
            )
            await system.add_urgent_message(
                message, references=asyncio.create_task(parse())
            )
            await parsing.wait()
            preparations = tuple(system._session._preparations)
            system._session.reset(discard=True)
            release.set()
            await asyncio.gather(*preparations, return_exceptions=True)
        assert not list((store.root / "manifests").glob("*.json"))
    finally:
        release.set()
        await system.shutdown()


async def test_slow_urgent_keeps_submission_order_through_final_response(
    tmp_path, monkeypatch
):
    system = configured_system(tmp_path, monkeypatch)
    configure_cli_hooks(system, monkeypatch)
    state = system.new_cli_session_state()
    state.is_first_input = False
    model_started, model_release = asyncio.Event(), asyncio.Event()
    parse_started, parse_release = asyncio.Event(), asyncio.Event()
    requests = []

    async def parse(text, **kwargs):
        if text == "urgent-1":
            parse_started.set()
            await parse_release.wait()
        return []

    async def model(messages, info):
        requests.append(list(messages))
        if len(requests) == 1:
            model_started.set()
            await model_release.wait()
        yield "done"

    async def create(*args, **kwargs):
        return Agent(FunctionModel(stream_function=model))

    monkeypatch.setattr("redlotus.agent_core.cli_controller.load_file_refs", parse)
    monkeypatch.setattr("redlotus.agent_core.system.create_coordinator_agent", create)
    try:
        async with asyncio.timeout(10):
            await system.process_cli_line("begin", state, wait_for_turn=False)
            await model_started.wait()
            first = asyncio.create_task(
                system.process_cli_line("/urgent urgent-1", state, wait_for_turn=False)
            )
            await parse_started.wait()
            for number in range(2, 7):
                await system.process_cli_line(
                    f"/urgent urgent-{number}", state, wait_for_turn=False
                )
            model_release.set()
            await asyncio.sleep(0.05)
            parse_release.set()
            await first
            await system._session.queue.join()
        prompts = [
            part.content[0] if isinstance(part.content, list) else part.content
            for messages in requests[1:]
            for part in messages[-1].parts
            if isinstance(part, UserPromptPart)
        ]
        assert prompts == [f"urgent-{n}" for n in range(1, 7)]
        assert len(system._memory.observations.order()) == 1
    finally:
        parse_release.set()
        model_release.set()
        await system.shutdown()


@pytest.mark.parametrize("control", ["/clear", "/stop", "/cd second"])
async def test_late_urgent_cannot_become_a_new_task(tmp_path, monkeypatch, control):
    system = configured_system(tmp_path, monkeypatch)
    configure_cli_hooks(system, monkeypatch)
    state = system.new_cli_session_state()
    state.is_first_input = False
    started, parsing, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
    seen = []
    (tmp_path / "second").mkdir()

    async def parse(text, **kwargs):
        if text == "OLD_ONLY":
            parsing.set()
            try:
                await release.wait()
            except asyncio.CancelledError:
                # A parser thread may finish after its waiter was cancelled.
                await release.wait()
        return []

    async def model(messages, info):
        text = messages[-1].parts[0].content[0]
        seen.append(text)
        if text == "old task":
            started.set()
            await asyncio.Event().wait()
        yield "reply:" + text

    async def create(*args, **kwargs):
        return Agent(FunctionModel(stream_function=model))

    monkeypatch.setattr("redlotus.agent_core.cli_controller.load_file_refs", parse)
    monkeypatch.setattr("redlotus.agent_core.system.create_coordinator_agent", create)
    monkeypatch.setattr(
        system, "generate_task_title", lambda text: asyncio.sleep(0, result="audit")
    )
    try:
        async with asyncio.timeout(10):
            await system.process_cli_line("old task", state, wait_for_turn=False)
            await started.wait()
            late = asyncio.create_task(
                system.process_cli_line("/urgent OLD_ONLY", state, wait_for_turn=False)
            )
            await parsing.wait()
            await system.process_cli_line("QUEUED_TASK", state, wait_for_turn=False)
            await system.process_cli_line(control, state, wait_for_turn=False)
            await system.process_cli_line("NEW_SESSION", state, wait_for_turn=True)
            release.set()
            await late
            await system._session.queue.join()
            await asyncio.sleep(0)
        expected = ["old task"]
        if control == "/stop":
            expected.append("QUEUED_TASK")
        assert seen == [*expected, "NEW_SESSION"]
        if control.startswith("/cd"):
            assert system.workspace.root == (tmp_path / "second").resolve()
    finally:
        release.set()
        await system.shutdown()


async def test_document_path_error_is_a_recoverable_tool_result(tmp_path):
    toolkit = BasicToolkit(None, workspace=WorkspaceContext.from_path(tmp_path))
    try:
        result = await toolkit.extract_text(str(tmp_path.parent / "not-authorized.pdf"))
        assert "Error" in str(result) and "read_reference" in str(result)
        result = await toolkit._references.read_reference("not-a-reference-id")
        assert "Error" in str(result)
    finally:
        await toolkit.close()


async def test_failed_urgent_does_not_drop_input_registered_at_later_boundary(
    tmp_path, monkeypatch
):
    system = configured_system(tmp_path, monkeypatch)
    configure_cli_hooks(system, monkeypatch)
    state = system.new_cli_session_state()
    state.is_first_input = False
    started, release_model, parsing, release_parse = (asyncio.Event() for _ in range(4))
    requests = []

    async def parse(text, **kwargs):
        if text == "bad attachment":
            parsing.set()
            await release_parse.wait()
            raise ValueError("invalid fixture")
        return []

    async def create(*args, **kwargs):
        async def model(messages, info):
            requests.append(messages[-1])
            if len(requests) == 1:
                started.set()
                await release_model.wait()
            yield "done"

        return Agent(FunctionModel(stream_function=model))

    monkeypatch.setattr("redlotus.agent_core.cli_controller.load_file_refs", parse)
    monkeypatch.setattr("redlotus.agent_core.system.create_coordinator_agent", create)
    try:
        async with asyncio.timeout(10):
            await system.process_cli_line("begin", state, wait_for_turn=False)
            await started.wait()
            await system.process_cli_line(
                "/urgent bad attachment", state, wait_for_turn=False
            )
            await parsing.wait()
            release_model.set()
            # Wait until the boundary has taken ownership of the first batch.
            while system._session._urgent:
                await asyncio.sleep(0)
            await system.process_cli_line(
                "/urgent keep this", state, wait_for_turn=False
            )
            release_parse.set()
            await system._session.queue.join()
        assert any(
            "keep this" in str(part.content)
            for request in requests
            for part in request.parts
            if isinstance(part, UserPromptPart)
        )
        assert system._cli_controller.last_rejected_input == "/urgent bad attachment"
    finally:
        release_parse.set()
        release_model.set()
        await system.shutdown()
