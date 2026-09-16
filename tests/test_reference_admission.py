import asyncio
import base64
import json

import httpx
import pytest
from PIL import Image
from pydantic_ai import Agent, BinaryContent
from pydantic_ai.messages import UserPromptPart
from pydantic_ai.models.function import FunctionModel

from redlotus.tools.interaction import load_file_refs
from redlotus.core.gateway import ModelTarget
from redlotus.core.gateway import create_model
from redlotus.core.config import references_dir
from redlotus.tools.references import DocumentReader
from redlotus.core.agents import WorkspaceContext
from redlotus.core.agents import workspace_context
from test_system import configured_system
from test_entries import configure_cli_hooks


async def test_all_originals_are_captured_before_slow_parsing(tmp_path, monkeypatch):
    files = []
    for number in range(5):
        path = tmp_path / f"reference-{number}.txt"
        path.write_text(f"original {number}", encoding="utf-8")
        files.append(path)
    started, release = asyncio.Event(), asyncio.Event()
    original_read = DocumentReader.read

    async def slow_read(reader, source, directory):
        started.set()
        await release.wait()
        return await original_read(reader, source, directory)

    monkeypatch.setattr(DocumentReader, "read", slow_read)
    with workspace_context(WorkspaceContext.from_path(tmp_path)):
        task = asyncio.create_task(load_file_refs(" ".join(f'@"{p}"' for p in files)))
        try:
            await asyncio.wait_for(started.wait(), 5)
            saved = list((references_dir() / "blobs").glob("*/source.txt"))
            assert len(saved) == 5
            files[-1].write_text("changed after admission", encoding="utf-8")
        finally:
            release.set()
            references = await task
    assert references[-1].parts[0].text == "original 4"


async def test_queued_input_captures_references_while_previous_turn_is_waiting(
    tmp_path, monkeypatch
):
    system = configured_system(tmp_path, monkeypatch)
    cli, hold, seen = system._cli_controller, asyncio.Event(), []
    state = cli.new_session_state()
    original = tmp_path / "queued.txt"
    original.write_text("value at admission", encoding="utf-8")

    async def start(text, state, *, references, **kwargs):
        seen.extend(await references)

    monkeypatch.setattr(cli, "_start_user_turn_from_raw_input", start)
    monkeypatch.setattr(
        "redlotus.core.console.app_config.missing_main_api_keys",
        lambda: (),
    )
    system._session.queue.submit(hold.wait)
    try:
        await cli.process_line(f'分析 @"{original}"', state, wait_for_turn=False)
        async with asyncio.timeout(5):
            while not list((references_dir() / "blobs").glob("*/source.txt")):
                await asyncio.sleep(0.01)
        original.write_text("changed while queued", encoding="utf-8")
    finally:
        hold.set()
        await system._session.queue.join()
        await system.shutdown()
    assert seen[0].parts[0].text == "value at admission"


async def test_twenty_unique_references_are_loaded_and_duplicates_do_not_use_slots(
    tmp_path,
):
    names = [f"资料{number}.txt" for number in range(20)]
    for number, name in enumerate(names):
        (tmp_path / name).write_text(f"body {number}", encoding="utf-8")
    text = "，".join(f"@{name}请阅读" for name in [*names, names[0]])
    refs = await load_file_refs(text, workspace=WorkspaceContext.from_path(tmp_path))
    assert [ref.name for ref in refs] == names
    assert [ref.parts[0].text for ref in refs] == [f"body {n}" for n in range(20)]


async def test_twenty_first_file_is_rejected_before_snapshot_reads(tmp_path):
    for number in range(21):
        (tmp_path / f"file{number}.txt").write_text("fixture", encoding="utf-8")
    text = "，".join(f"@file{number}.txt" for number in range(21))
    with pytest.raises(ValueError, match="20.*21"):
        await load_file_refs(text, workspace=WorkspaceContext.from_path(tmp_path))
    assert not (references_dir() / "blobs").exists()


async def test_cli_first_http_request_contains_document_and_original_image(
    tmp_path, monkeypatch, capsys
):
    system = configured_system(tmp_path, monkeypatch)
    configure_cli_hooks(system, monkeypatch)
    state = system.new_cli_session_state()
    state.is_first_input = False
    body = "# 审稿意见\n完整正文\n文件内的 @not-admitted.txt 是引用资料。"
    (tmp_path / "审稿意见.md").write_bytes(body.encode("utf-8"))
    Image.new("RGB", (3, 2), "red").save(tmp_path / "图片.png")
    original = (tmp_path / "图片.png").read_bytes()
    raw = "@审稿意见.md解读这个文档，@图片.png告诉我修改了什么"
    payloads = []

    def respond(request):
        payloads.append(json.loads(request.content))
        chunks = [
            {"delta": {"role": "assistant", "content": "done"}, "finish_reason": None},
            {"delta": {}, "finish_reason": "stop"},
        ]
        events = "".join(
            "data: "
            + json.dumps(
                {
                    "id": "reference-test",
                    "object": "chat.completion.chunk",
                    "created": 1,
                    "model": "deepseek-flash",
                    "choices": [{"index": 0, **chunk}],
                }
            )
            + "\n\n"
            for chunk in chunks
        )
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=events + "data: [DONE]\n\n",
        )

    target = ModelTarget(
        "deepseek-flash",
        "openai-chat",
        "https://offline.invalid/v1",
        "test-only",
        json.dumps(
            {
                "connect_timeout": 10,
                "limits": {"max_files": 20, "max_file_bytes": 20_000_000},
                "context": {},
                "settings": {"thinking": "disabled", "max_tokens": 64},
            }
        ),
        30,
    )
    try:
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            monkeypatch.setattr("pydantic_ai.models.ALLOW_MODEL_REQUESTS", True)
            monkeypatch.setattr(
                "redlotus.core.gateway.get_client",
                lambda key, factory: client,
            )

            async def create(*args, **kwargs):
                return Agent(create_model(target))

            monkeypatch.setattr(
                "redlotus.core.system.create_coordinator_agent", create
            )
            async with asyncio.timeout(5):
                await system.process_cli_line(raw, state, wait_for_turn=True)
        assert system.last_turn_error is None
        assert not system.has_current_turn
        assert len(payloads) == 1
        parts = next(
            message["content"]
            for message in payloads[0]["messages"]
            if message["role"] == "user"
        )
        texts = [part["text"] for part in parts if part["type"] == "text"]
        assert texts[0] == raw
        assert any(body in text for text in texts)
        assert any("以下内容是引用资料" in text for text in texts)
        pictures = [
            part["image_url"]["url"] for part in parts if part["type"] == "image_url"
        ]
        assert len(pictures) == 1
        assert pictures[0].startswith("data:image/png;base64,")
        assert base64.b64decode(pictures[0].split(",", 1)[1]) == original
        event = system._session_file.pending_turns(0)[0]
        assert len(event["reference_ids"]) == 2 and event["status"] == "success"
        assert "已解析 2 个引用文件" in capsys.readouterr().out
    finally:
        await system.shutdown()


@pytest.mark.parametrize("failure", ["missing", "invalid_image", "too_many"])
async def test_rejected_references_do_not_call_model_or_block_next_turn(
    tmp_path, monkeypatch, failure
):
    system = configured_system(tmp_path, monkeypatch)
    configure_cli_hooks(system, monkeypatch)
    state = system.new_cli_session_state()
    state.is_first_input = False
    (tmp_path / "good.md").write_text("valid body", encoding="utf-8")
    Image.new("RGB", (2, 2), "blue").save(tmp_path / "good.png")
    (tmp_path / "broken.png").write_bytes(b"not an image")
    if failure == "too_many":
        for number in range(21):
            (tmp_path / f"file{number}.txt").write_text("fixture", encoding="utf-8")
        raw = "，".join(f"@file{number}.txt" for number in range(21))
    else:
        name = "missing.png" if failure == "missing" else "broken.png"
        raw = f"@good.md说明，@{name}说明"
    requests = []

    async def create(*args, **kwargs):
        async def model(messages, info):
            requests.append(messages[-1])
            yield "done"

        return Agent(FunctionModel(stream_function=model))

    monkeypatch.setattr("redlotus.core.system.create_coordinator_agent", create)
    try:
        async with asyncio.timeout(5):
            await system.process_cli_line(raw, state, wait_for_turn=True)
            assert not requests and not state.history.messages
            assert system._cli_controller.last_rejected_input == raw
            await system.process_cli_line(
                "@good.md说明，@good.png说明", state, wait_for_turn=True
            )
        assert len(requests) == 1
        content = next(
            part.content
            for part in requests[0].parts
            if isinstance(part, UserPromptPart)
        )
        assert any("valid body" in item for item in content if isinstance(item, str))
        assert sum(isinstance(item, BinaryContent) for item in content) == 1
        assert not system.has_current_turn and not system._session.queue.current
    finally:
        await system.shutdown()


async def test_cancelled_reference_preparation_does_not_block_queued_turn(
    tmp_path, monkeypatch
):
    system = configured_system(tmp_path, monkeypatch)
    configure_cli_hooks(system, monkeypatch)
    state = system.new_cli_session_state()
    state.is_first_input = False
    (tmp_path / "slow.md").write_text("slow reference", encoding="utf-8")
    (tmp_path / "next.md").write_text("next reference", encoding="utf-8")
    started = asyncio.Event()
    original_read = DocumentReader.read
    inputs = []

    async def read(reader, source, directory):
        if source.read_text(encoding="utf-8") == "slow reference":
            started.set()
            await asyncio.Event().wait()
        return await original_read(reader, source, directory)

    async def create(*args, **kwargs):
        async def model(messages, info):
            inputs.append(messages[-1].parts[0].content[0])
            yield "done"

        return Agent(FunctionModel(stream_function=model))

    monkeypatch.setattr(DocumentReader, "read", read)
    monkeypatch.setattr("redlotus.core.system.create_coordinator_agent", create)
    try:
        async with asyncio.timeout(5):
            await system.process_cli_line("@slow.md请阅读", state, wait_for_turn=False)
            await started.wait()
            await system.process_cli_line("@next.md请阅读", state, wait_for_turn=False)
            await system.cancel_current_turn()
            await system._session.queue.join()
        assert inputs == ["@next.md请阅读"]
        assert system._session.queue.current is None and not system.has_current_turn
    finally:
        await system.shutdown()
