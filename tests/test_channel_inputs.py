import asyncio
from datetime import datetime
from types import SimpleNamespace

import pytest
from pydantic_ai import BinaryContent

from redlotus.api.base import BotBase
from redlotus.api.QQ import QQBot
from redlotus.api.WeChat import WeChatAgentBot
from redlotus.api import media as qq
from redlotus.runtime.resources import WorkspaceContext
from redlotus.sessions.control import UserMessage
from redlotus.tools.references import ReferenceStore


@pytest.mark.asyncio
async def test_attachment_only_and_multiple_files_are_snapshotted(phone):
    bot, state, send, replies, calls = phone
    async def prepare():
        return [BinaryContent(b"first", media_type="text/plain", identifier="one.txt"),
                BinaryContent(b"second", media_type="text/plain", identifier="two.txt")]
    await bot.dispatch_user_message("wx_owner", UserMessage(""), send, prepare=prepare)
    await state.queue.join()
    assert len(calls) == 1
    assert [ref.name for ref in calls[0].references] == ["one.txt", "two.txt"]
    assert "first" in calls[0].references[0].parts[0].text
    assert calls[0].references[0].snapshot.read_bytes() == b"first"



@pytest.mark.asyncio
async def test_image_only_reaches_model_with_real_image_bytes(phone):
    import io
    from PIL import Image
    bot, state, send, replies, calls = phone
    stream = io.BytesIO()
    Image.new("RGB", (2, 2), "red").save(stream, format="PNG")
    data = stream.getvalue()
    await bot.dispatch_user_message("wx_owner", UserMessage("", attachments=[
        BinaryContent(data, media_type="image/png", identifier="picture.png")]), send)
    await state.queue.join()
    assert calls[0].references[0].snapshot.read_bytes() == data
    assert calls[0].references[0].parts[0].kind == "image"
    assert replies[-1] == "final answer"



@pytest.mark.asyncio
async def test_reference_preserves_supplied_mime_and_original_filename(phone):
    import io
    from PIL import Image
    bot, state, send, replies, calls = phone
    stream = io.BytesIO()
    Image.new("RGB", (2, 2), "blue").save(stream, format="PNG")
    message = UserMessage("", attachments=[BinaryContent(
        stream.getvalue(), media_type="image/png", identifier="original.blob")])
    await state.agent.toolkit._references.prepare_message(message)
    assert message.references[0].name == "original.blob"
    assert message.references[0].media_type == "image/png"
    assert message.references[0].parts[0].kind == "image"



@pytest.mark.asyncio
async def test_attachment_size_failure_names_item(phone, isolated_config):
    bot, state, send, replies, calls = phone
    isolated_config["input_limits"]["defaults"]["max_file_bytes"] = 2
    message = UserMessage("", attachments=[BinaryContent(b"too large", media_type="text/plain", identifier="large.txt")])
    await bot.dispatch_user_message("wx_owner", message, send)
    await state.queue.join()
    assert calls == []
    assert "large.txt" in replies[-1]



@pytest.mark.asyncio
async def test_same_bytes_new_attachment_keeps_its_filename_and_mime(phone):
    bot, state, send, replies, calls = phone
    store = state.agent.toolkit._references
    first = UserMessage("", attachments=[BinaryContent(b"same content", media_type="text/plain", identifier="first.txt")])
    renamed = UserMessage("", attachments=[BinaryContent(b"same content", media_type="text/plain", identifier="renamed.txt")])
    new_mime = UserMessage("", attachments=[BinaryContent(b"same content", media_type="text/markdown", identifier="renamed.txt")])
    for message in (first, renamed, new_mime):
        await store.prepare_message(message)
    refs = [message.references[0] for message in (first, renamed, new_mime)]
    assert [ref.name for ref in refs] == ["first.txt", "renamed.txt", "renamed.txt"]
    assert [ref.media_type for ref in refs] == ["text/plain", "text/plain", "text/markdown"]
    assert len({ref.id for ref in refs}) == 3
    assert len({ref.snapshot.parent for ref in refs}) == 1
    existing = UserMessage("", attachments=[BinaryContent(b"same content", media_type="text/plain", identifier=refs[0].id)])
    await store.prepare_message(existing)
    assert existing.references[0].id == refs[0].id
    assert existing.references[0].name == "first.txt"



@pytest.mark.asyncio
async def test_shared_wav_blob_keeps_each_native_part_mime_and_reparses_old_manifest(phone):
    import io
    import json
    import wave
    bot, state, send, replies, calls = phone
    store = state.agent.toolkit._references
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(8000)
        audio.writeframes(b"\x00\x00" * 8)
    refs = []
    for mime in ("audio/x-wav", "audio/wav"):
        message = UserMessage("", attachments=[BinaryContent(buffer.getvalue(), media_type=mime, identifier="sound.wav")])
        await store.prepare_message(message)
        refs.append(message.references[0])
    assert [ref.parts[0].media_type for ref in refs] == ["audio/x-wav", "audio/wav"]
    assert refs[0].snapshot == refs[1].snapshot
    old = refs[1].manifest()
    old["parser_version"] = 5
    old["parts"][0]["media_type"] = "audio/x-wav"
    (store.root / "manifests" / f"{refs[1].id}.json").write_text(json.dumps(old), encoding="utf-8")
    (refs[1].snapshot.parent / "parts-v5.wav.json").write_text(json.dumps(old["parts"]), encoding="utf-8")
    registered = UserMessage("", attachments=[BinaryContent(buffer.getvalue(), media_type="audio/wav", identifier=refs[1].id)])
    await store.prepare_message(registered)
    assert registered.references[0].id == refs[1].id
    assert registered.references[0].parts[0].media_type == "audio/wav"
    assert registered.references[0].parser_version > 5



@pytest.mark.asyncio
async def test_cli_uses_shared_start_and_snapshots_reference(isolated_config, tmp_path, monkeypatch):
    from redlotus.ui.console import AgentCliController
    from redlotus.sessions.control import SessionController
    from redlotus.sessions.context import ChatHistory
    path = tmp_path / "notes.txt"
    path.write_text("local attachment", encoding="utf-8")
    inputs, received = SessionController(), []
    class System:
        workspace = WorkspaceContext.from_path(tmp_path)
        _session = inputs
        _session_file = None
        last_rejected_input = None
        toolkit = SimpleNamespace(_references=ReferenceStore(workspace))
        def _start_user_turn(self, message, history, *, turn_id):
            async def run():
                received.append((message, turn_id))
                return "done"
            return asyncio.create_task(run())
    cli = AgentCliController(System())
    async def publish(history):
        pass
    monkeypatch.setattr(cli, "_publish_context_usage", publish)
    state = cli.new_session_state()
    state.is_first_input = False
    text = '  code:\n    @"' + str(path) + '"\n'
    assert await cli.process_line(text, state, wait_for_turn=True, input_id="cli-input") == "continue"
    assert received[0][1] == "cli-input"
    assert received[0][0].text == text
    assert state is cli.system._session
    assert received[0][0].references[0].snapshot.read_bytes() == b"local attachment"



@pytest.mark.asyncio
async def test_cli_reference_errors_keep_each_missing_filename(isolated_config, tmp_path):
    from redlotus.sessions.control import load_file_refs
    with pytest.raises(ValueError) as caught:
        await load_file_refs('@"missing-one.txt" @"missing-two.txt"', workspace=WorkspaceContext.from_path(tmp_path))
    assert "missing-one.txt" in str(caught.value)
    assert "missing-two.txt" in str(caught.value)



@pytest.mark.asyncio
@pytest.mark.parametrize("invalid", [False, True])
async def test_supplied_snapshot_reference_is_parsed_and_named_before_start(phone, invalid):
    from redlotus.runtime.network import ModelInputPolicy
    bot, state, send, replies, calls = phone
    store = state.agent.toolkit._references
    name = "broken.png" if invalid else "captured.txt"
    reference = await store.capture_bytes(b"snapshot bytes", name=name, source="fixture", policy=ModelInputPolicy.for_role())
    assert not reference.parts
    await bot.dispatch_user_message("wx_owner", UserMessage("", references=[reference]), send)
    await state.queue.join()
    if invalid:
        assert not calls
        assert name in replies[-1]
    else:
        assert calls[0].references[0].parts[0].text == "snapshot bytes"



@pytest.mark.asyncio
@pytest.mark.parametrize("format", ["pdf", "csv", "xlsx"])
async def test_extensionless_attachment_uses_mime_parser_and_keeps_name(phone, format):
    import io
    bot, state, send, replies, calls = phone
    if format == "pdf":
        import pymupdf
        document = pymupdf.open()
        document.new_page().insert_text((72, 72), "extensionless evidence")
        data, mime, locator = document.tobytes(), "application/pdf", "Page"
        document.close()
    elif format == "xlsx":
        from openpyxl import Workbook
        document, buffer = Workbook(), io.BytesIO()
        document.active.append(["extensionless evidence"])
        document.save(buffer)
        document.close()
        data, mime, locator = buffer.getvalue(), "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", "Sheet"
    else:
        data, mime, locator = b"name,value\nexample,extensionless evidence\n", "text/csv", "CSV rows and columns"
    await bot.dispatch_user_message("wx_owner", UserMessage("", attachments=[
        BinaryContent(data, media_type=mime, identifier="original upload")]), send)
    await state.queue.join()
    assert len(calls) == 1, replies
    reference = calls[0].references[0]
    assert reference.name == "original upload"
    assert reference.media_type == mime
    assert any(locator in part.locator and "extensionless evidence" in part.text for part in reference.parts)
