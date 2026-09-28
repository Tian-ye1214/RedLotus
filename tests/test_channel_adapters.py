import asyncio
import base64
from datetime import datetime
from functools import partial
from types import SimpleNamespace

import pytest
from pydantic_ai import BinaryContent

from redlotus.api.base import BotBase
from redlotus.api.QQ import QQBot
from redlotus.api.WeChat import WeChatAgentBot
from redlotus.api import media as qq
from redlotus.runtime.resources import WorkspaceContext
from redlotus.sessions.control import UserMessage
from redlotus.TTS import AudioSegment
from redlotus.tools.references import ReferenceStore


def test_phone_preserves_code_spacing():
    text = "  code:\n    if x:\n        y()\n"
    assert WeChatAgentBot().clean_text(text) == text
    bot = QQBot.__new__(QQBot)
    assert bot.clean_text("[CQ:at,qq=1]" + text) == text



@pytest.mark.asyncio
async def test_qq_mixed_media_preserves_order_and_names(monkeypatch):
    event = SimpleNamespace(message=[
        {"type": "file", "data": {"file_id": "a", "file": "first.txt"}},
        {"type": "image", "data": {"url": "https://unused.invalid/i", "file": "second.png"}},
        {"type": "file", "data": {"file_id": "b", "file": "third.txt"}},
    ], raw_message="")
    def download(url, filename=""):
        return BinaryContent(b"bytes", media_type="text/plain", identifier=filename)
    async def file_download(api, event, fid, name):
        return download("", name)
    monkeypatch.setattr(qq, "download_to_binary", download)
    monkeypatch.setattr(qq, "file_id_to_binary", file_download)
    result = await qq.extract_media(None, event)
    assert [item.identifier for item in result] == ["first.txt", "second.png", "third.txt"]



@pytest.mark.asyncio
async def test_wechat_raw_order_names_and_actual_mime():
    from wechatbot.types import IncomingMessage, FileContent, ImageContent, DownloadedMedia
    message = IncomingMessage("owner", "", "file", datetime.now(),
        files=[FileContent(file_name="first.txt"), FileContent(file_name="third.txt")],
        images=[ImageContent()], raw={"item_list": [
            {"type": 4, "file_item": {"file_name": "first.txt"}},
            {"type": 2, "image_item": {"url": "synthetic"}},
            {"type": 4, "file_item": {"file_name": "third.txt"}},
        ]})
    async def download(single):
        if single.files:
            return DownloadedMedia(b"text", "file", single.files[0].file_name)
        return DownloadedMedia(b"\x89PNG\r\n\x1a\nsynthetic", "image", "original.png")
    result = await WeChatAgentBot()._download_attachments(SimpleNamespace(download=download), message)
    assert [item.identifier for item in result] == ["first.txt", "original.png", "third.txt"]
    assert result[1].media_type == "image/png"



@pytest.mark.parametrize("bot_type,identity,allowed", [
    (QQBot, "private_42", True), (QQBot, "private_43", False),
    (QQBot, "group_42", False), (WeChatAgentBot, "wx_owner", True),
    (WeChatAgentBot, "wx_other", False),
])
def test_only_bound_private_owner_has_privileges(isolated_config, monkeypatch, bot_type, identity, allowed):
    import redlotus.core.system as system
    from redlotus.runtime import logging
    isolated_config["bot"] = {"owner_channels": {"qq": ["42"], "wechat": "owner"}}
    captured = []
    def construct(**kwargs):
        captured.append(kwargs)
        return SimpleNamespace(workspace=None, set_ask_user_handler=lambda h: None,
                               toolkit=SimpleNamespace(set_task_directory=lambda d: None))
    monkeypatch.setattr(system, "AgentSystem", construct)
    monkeypatch.setattr(logging, "prepare_log_dir", lambda w: None)
    monkeypatch.setattr(logging, "activate_log_dir", lambda d: None)
    monkeypatch.setattr(logging, "prune_old_logs", lambda: None)
    bot = bot_type.__new__(bot_type)
    BotBase.__init__(bot)
    bot._agent_for_session(identity)
    assert captured[0]["owner_memory_allowed"] is allowed



def test_channels_keep_separate_conversations():
    qq_bot = QQBot.__new__(QQBot)
    BotBase.__init__(qq_bot)
    wx_bot = WeChatAgentBot()
    states = [qq_bot._session("private_42"), qq_bot._session("group_42"), wx_bot._session("wx_42")]
    assert len({id(state.history) for state in states}) == 3
    assert len({id(state) for state in states}) == 3



@pytest.mark.asyncio
async def test_wechat_image_without_filename_uses_actual_png_mime():
    from wechatbot.types import IncomingMessage, ImageContent, DownloadedMedia
    message = IncomingMessage("owner", "", "image", datetime.now(), images=[ImageContent()])
    async def download(single):
        return DownloadedMedia(b"\x89PNG\r\n\x1a\nsynthetic", "image")
    result = await WeChatAgentBot()._download_attachments(SimpleNamespace(download=download), message)
    assert result[0].media_type == "image/png"



@pytest.mark.asyncio
async def test_qq_raw_media_order_and_base64_failure_named(monkeypatch):
    event = SimpleNamespace(message=None, raw_message="[CQ:file,file=first.txt,file_id=a][CQ:image,file=base64://bad,name=bad.png]")
    async def file_download(*args):
        return BinaryContent(b"first", media_type="text/plain", identifier="first.txt")
    monkeypatch.setattr(qq, "file_id_to_binary", file_download)
    with pytest.raises(ValueError, match="bad.png"):
        await qq.extract_media(None, event)



@pytest.mark.asyncio
async def test_qq_accepts_source_file_for_shared_parser(monkeypatch):
    async def url(file_id):
        return "https://unused.invalid/source"
    monkeypatch.setattr(qq, "download_to_binary", lambda url, filename:
                        BinaryContent(b"print('hello')", media_type="text/x-python", identifier=filename))
    event = SimpleNamespace(is_group_msg=lambda: False, raw_message="", message=[
        {"type": "file", "data": {"file_id": "source", "file": "main.py"}}])
    result = await qq.extract_media(SimpleNamespace(get_private_file_url=url), event)
    assert result[0].identifier == "main.py"



@pytest.mark.asyncio
async def test_sdk_entrypoints_keep_text_and_image_only_messages(phone, monkeypatch):
    import base64
    import io
    from PIL import Image
    from wechatbot.types import IncomingMessage, FileContent, DownloadedMedia
    bot, state, send, replies, calls = phone
    text = "line one\n    code()\n"
    message = IncomingMessage("owner", text, "file", datetime.now(), files=[FileContent(file_name="notes.txt")])
    async def download(single):
        return DownloadedMedia(b"wechat attachment", "file", "notes.txt")
    async def reply(message, text):
        await send(text)
    await bot._handle_message(SimpleNamespace(download=download, reply=reply), message)
    await state.queue.join()
    assert calls[0].text == text
    assert calls[0].references[0].snapshot.read_bytes() == b"wechat attachment"
    qq_bot = QQBot.__new__(QQBot)
    BotBase.__init__(qq_bot)
    qq_bot._sessions["private_owner"] = state
    qq_bot._bot_client = SimpleNamespace(api=None)
    monkeypatch.setattr(qq_bot, "_is_at_me", lambda event: True)
    stream = io.BytesIO()
    Image.new("RGB", (2, 2), "green").save(stream, format="PNG")
    event = SimpleNamespace(raw_message="[CQ:image,file=picture.png]", is_group_msg=lambda: False,
        user_id="owner", reply=send, message=[{"type": "image", "data": {
            "file": "base64://" + base64.b64encode(stream.getvalue()).decode(), "name": "picture.png"}}])
    await qq_bot._handle_message(event)
    await state.queue.join()
    assert calls[1].text == ""
    assert calls[1].references[0].name == "picture.png"
    assert calls[1].references[0].snapshot.read_bytes() == stream.getvalue()



@pytest.mark.asyncio
async def test_wechat_failure_names_second_file_and_returns_no_partial_list():
    from wechatbot.types import IncomingMessage, FileContent, DownloadedMedia
    message = IncomingMessage("owner", "", "file", datetime.now(),
        files=[FileContent(file_name="first.txt"), FileContent(file_name="second.txt")])
    downloaded = []
    async def download(single):
        name = single.files[0].file_name
        downloaded.append(name)
        if name == "second.txt":
            raise OSError("synthetic network failure")
        return DownloadedMedia(b"first", "file", name)
    with pytest.raises(ValueError, match="second.txt"):
        await WeChatAgentBot()._download_attachments(SimpleNamespace(download=download), message)
    assert downloaded == ["first.txt", "second.txt"]



@pytest.mark.asyncio
@pytest.mark.parametrize("command", ["/stop", "/clear"])
async def test_qq_group_mention_followed_by_space_recognizes_command(phone, monkeypatch, command):
    bot, state, send, replies, calls = phone
    qq_bot = QQBot.__new__(QQBot)
    BotBase.__init__(qq_bot)
    qq_bot._sessions["group_42"] = state
    qq_bot._bot_client = SimpleNamespace(api=None)
    monkeypatch.setattr(qq_bot, "_is_at_me", lambda event: True)
    state.question = asyncio.get_running_loop().create_future()
    question = state.question
    async def reply(text, *, at):
        assert at is False
        await send(text)
    event = SimpleNamespace(raw_message="[CQ:at,qq=99] " + command,
        is_group_msg=lambda: True, group_id="42", reply=reply, message=[])
    await qq_bot._handle_message(event)
    assert question.cancelled()
    assert calls == []
    assert len(replies) == 1



def test_bot_base_requires_platform_adapter():
    with pytest.raises(TypeError, match="adapt_message"):
        BotBase()



@pytest.mark.asyncio
@pytest.mark.parametrize("path,value", [
    (("models", "main", "name"), "openai:chosen"),
    (("BASE_URL",), "https://synthetic.invalid/v1"),
    (("API_KEY",), "synthetic-chosen-key"),
])
async def test_startup_configuration_fills_only_missing_field_and_commits_once(isolated_config, monkeypatch, path, value):
    from copy import deepcopy
    from redlotus.api import base
    target = isolated_config
    for key in path[:-1]:
        target = target[key]
    del target[path[-1]]
    original, writes, prompts = deepcopy(isolated_config), [], []
    async def ask(text, **kwargs):
        prompts.append(text)
        return value if len(prompts) == 1 else "y"
    def update(apply):
        saved = deepcopy(original)
        apply(saved)
        writes.append(saved)
    monkeypatch.setattr(base, "update_config", update)
    assert await base.prepare_startup_configuration(ask=ask, emit=lambda text: None)
    assert len(writes) == 1
    expected = deepcopy(original)
    base.ConfigurationSetup.assign(expected, path, value)
    assert writes[0] == expected
    assert len(prompts) == 2
    assert ".".join(path) in prompts[0]



@pytest.mark.asyncio
@pytest.mark.parametrize("interactive", [False, True])
async def test_startup_configuration_invalid_policy_never_opens_dialog(isolated_config, monkeypatch, interactive):
    from redlotus.api import base
    isolated_config["models"]["main"]["name"] = 12
    prompts = []
    async def ask(text, **kwargs):
        prompts.append(text)
        return "unused"
    monkeypatch.setattr(base, "configuration_prompt_available", lambda: False)
    with pytest.raises(base.ConfigError):
        await base.prepare_startup_configuration(ask=ask if interactive else None, emit=lambda text: None)
    assert prompts == []


@pytest.mark.asyncio
async def test_qq_record_is_voice_and_sends_record_in_original_context(tmp_path, monkeypatch):
    monkeypatch.setenv("LOG_FORMAT", "%(message)s")
    monkeypatch.setenv("LOG_FILE_PATH", str(tmp_path / "ncatbot-logs"))
    monkeypatch.setenv("NCATBOT_CONFIG_PATH", str(tmp_path / "missing-ncatbot.yaml"))
    from ncatbot.core.event.message_segment import Record

    bot = QQBot.__new__(QQBot)
    BotBase.__init__(bot)
    bot._bot_client = SimpleNamespace(api=object())
    monkeypatch.setattr(bot, "_is_at_me", lambda event: True)
    calls = []

    async def reply(text=None, *, at=None, rtf=None):
        calls.append((text, at, rtf))

    event = SimpleNamespace(
        raw_message="[CQ:at,qq=99] hello [CQ:record,file=voice.silk]",
        message=[{"type": "record", "data": {"file": "voice.silk"}}],
        is_group_msg=lambda: True, group_id="42", user_id="sender", reply=reply,
    )
    identity, message, send, prepare = bot.adapt_message(event)
    assert identity == "group_42"
    assert message.text == " hello "
    assert message.voice is True
    assert prepare is not None
    assert isinstance(send, partial)
    assert send.speech_format == "silk"
    raw = b"\x02#!SILK_V3 synthetic"
    await send.speech_sender(AudioSegment(raw, "silk", 1.0, 24000))
    assert len(calls) == 1
    assert calls[0][:2] == (None, False)
    assert isinstance(calls[0][2].messages[0], Record)
    assert calls[0][2].messages[0].file.startswith("base64://")
    payload = calls[0][2].to_list()[0]
    assert payload["type"] == "record"
    assert payload["data"]["file"].startswith("base64://")
    assert base64.b64decode(payload["data"]["file"][9:]) == raw


@pytest.mark.asyncio
async def test_qq_private_record_unknown_result_is_not_resent(tmp_path, monkeypatch):
    bot = QQBot.__new__(QQBot)
    BotBase.__init__(bot)
    bot._bot_client = SimpleNamespace(api=object())
    monkeypatch.setattr(bot, "_is_at_me", lambda event: True)
    calls = []

    async def reply(text=None, *, rtf=None):
        calls.append((text, rtf))
        raise TimeoutError("delivery unknown")

    event = SimpleNamespace(
        raw_message="[CQ:record,file=voice.silk]", message=None,
        is_group_msg=lambda: False, user_id="owner", reply=reply,
    )
    identity, message, send, prepare = bot.adapt_message(event)
    assert identity == "private_owner"
    assert message.text == ""
    assert message.voice is True
    assert prepare is not None
    assert isinstance(send, partial)
    with pytest.raises(TimeoutError, match="delivery unknown"):
        await send.speech_sender(AudioSegment(b"\x02#!SILK_V3 synthetic", "silk", 1.0, 24000))
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_wechat_voice_keeps_only_user_text_and_silk_mime(isolated_config):
    from wechatbot.types import DownloadedMedia, IncomingMessage, VoiceContent

    msg = IncomingMessage("recipient", "SDK transcript", "voice", datetime.now(),
        voices=[VoiceContent()], raw={"item_list": [
            {"type": 1, "text_item": {"text": "typed text"}},
            {"type": 3, "voice_item": {"text": "SDK transcript"}},
        ]})
    async def download(single):
        return DownloadedMedia(b"\x02#!SILK_V3 synthetic", "voice", format="silk")
    async def reply(message, text):
        pass
    sdk = SimpleNamespace(download=download, reply=reply, get_credentials=lambda: None)
    identity, message, send, prepare = WeChatAgentBot().adapt_message(sdk, msg)
    assert identity == "wx_recipient"
    assert message.text == "typed text"
    assert message.voice is True
    assert isinstance(send, partial)
    assert send.speech_format == "wav"
    assert (await prepare())[0].media_type == "audio/silk"
    msg.raw = {"item_list": [{"type": 3, "voice_item": {"text": "SDK transcript"}}]}
    assert WeChatAgentBot().adapt_message(sdk, msg)[1].text == ""


@pytest.mark.asyncio
async def test_wechat_unverified_target_sends_wav_attachment(isolated_config, tmp_path):
    from wechatbot.types import Credentials, IncomingMessage

    isolated_config["speech"] = {"wechat_silk_verified_targets": ["another:recipient"]}
    msg = IncomingMessage("recipient", "hello", "text", datetime.now())
    uploads = []

    async def reply_media(message, content):
        uploads.append((message, content))

    sdk = SimpleNamespace(
        reply=lambda *args: None, reply_media=reply_media,
        get_credentials=lambda: Credentials("token", "https://unused.invalid", "account", "bot"),
    )
    _, _, send, _ = WeChatAgentBot().adapt_message(sdk, msg)
    assert send.speech_format == "wav"
    await send.speech_sender(AudioSegment(b"RIFF synthetic", "wav", 1.0, 24000))
    assert len(uploads) == 1
    assert uploads[0][0] is msg
    assert uploads[0][1]["file"] == b"RIFF synthetic"
    assert uploads[0][1]["file_name"].endswith(".wav")


@pytest.mark.asyncio
async def test_wechat_verified_target_sends_native_voice_once(isolated_config, tmp_path):
    from wechatbot.types import CDNMedia, Credentials, IncomingMessage, UploadResult

    isolated_config["speech"] = {"wechat_silk_verified_targets": ["account:recipient"]}
    msg = IncomingMessage("recipient", "hello", "text", datetime.now(), _context_token="original-context")
    calls = []

    async def send_media_buffer(user_id, context_token, data, media_type, build_item):
        item = build_item(UploadResult(CDNMedia("encrypted", "aeskey", 1), b"key", len(data)))
        calls.append((user_id, context_token, data, media_type, item))
        raise TimeoutError("delivery unknown")

    async def reply_media(*args):
        pytest.fail("native voice must not fall back after an unknown result")

    sdk = SimpleNamespace(
        reply=lambda *args: None, reply_media=reply_media,
        get_credentials=lambda: Credentials("token", "https://unused.invalid", "account", "bot"),
        _send_media_buffer=send_media_buffer,
    )
    _, _, send, _ = WeChatAgentBot().adapt_message(sdk, msg)
    assert send.speech_format == "silk"
    other = IncomingMessage("someone-else", "hello", "text", datetime.now())
    assert WeChatAgentBot().adapt_message(sdk, other)[2].speech_format == "wav"
    raw = b"\x02#!SILK_V3 synthetic"
    with pytest.raises(TimeoutError, match="delivery unknown"):
        await send.speech_sender(AudioSegment(raw, "silk", 1.25, 24000))
    assert len(calls) == 1
    assert calls[0][:4] == ("recipient", "original-context", raw, 4)
    assert calls[0][4] == {"type": 3, "voice_item": {
        "media": {"encrypt_query_param": "encrypted", "aes_key": "aeskey", "encrypt_type": 1},
        "encode_type": 6, "playtime": 1250, "sample_rate": 24000, "bits_per_sample": 16,
    }}


@pytest.mark.asyncio
@pytest.mark.parametrize("native", [False, True])
async def test_wechat_cancel_in_memory_send_does_not_retry(isolated_config, tmp_path, monkeypatch, native):
    from wechatbot.types import Credentials, IncomingMessage

    isolated_config["speech"] = {"wechat_silk_verified_targets": ["account:recipient"]}
    started = asyncio.Event()
    sent = []
    async def send(*args):
        sent.append(args)
        started.set()
        await asyncio.Event().wait()
    sdk = SimpleNamespace(reply_media=send, _send_media_buffer=send,
        get_credentials=lambda: Credentials("token", "https://unused.invalid", "account", "bot"))
    msg = IncomingMessage("recipient", "hello", "text", datetime.now(), _context_token="original-context")
    task = asyncio.create_task(WeChatAgentBot().send_voice(
        sdk, msg, native, AudioSegment(b"synthetic audio", "silk" if native else "wav", 1, 24000)))
    await asyncio.wait_for(started.wait(), 2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert len(sent) == 1
    assert not list(tmp_path.iterdir())


@pytest.mark.asyncio
async def test_qq_record_url_and_base64_keep_silk_bytes_and_order(monkeypatch):
    silk = b"\x02#!SILK_V3 synthetic"
    seen = []
    def download(url, filename=""):
        seen.append(url)
        return BinaryContent(silk, media_type="application/octet-stream", identifier=filename)
    async def unexpected(**kwargs):
        pytest.fail("valid SILK should not request NapCat conversion")
    monkeypatch.setattr(qq, "download_to_binary", download)
    event = SimpleNamespace(message=[
        {"type": "record", "data": {"name": "first.silk", "file": "first.silk", "url": "https://unused.invalid/first"}},
        {"type": "file", "data": {"file_id": "f", "file": "middle.txt"}},
        {"type": "record", "data": {"name": "last.silk", "file": "base64://" + base64.b64encode(silk).decode()}},
    ], raw_message="", is_group_msg=lambda: False)
    async def file_url(file_id):
        return "https://unused.invalid/middle"
    result = await qq.extract_media(SimpleNamespace(get_record=unexpected, get_private_file_url=file_url), event)
    assert [item.identifier for item in result] == ["first.silk", "middle.txt", "last.silk"]
    assert [item.media_type for item in result] == ["audio/silk", "application/octet-stream", "audio/silk"]
    assert result[0].data == result[2].data == silk
    assert seen == ["https://unused.invalid/first", "https://unused.invalid/middle"]


@pytest.mark.asyncio
async def test_qq_record_uses_napcat_wav_conversion_when_direct_silk_unavailable(tmp_path):
    import wave
    converted = tmp_path / "converted.wav"
    with wave.open(str(converted), "wb") as writer:
        writer.setnchannels(1)
        writer.setsampwidth(2)
        writer.setframerate(16000)
        writer.writeframes(b"\x00\x00" * 160)
    calls = []
    async def get_record(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(file=str(converted), url=None, base64=None)
    event = SimpleNamespace(message=None, raw_message="[CQ:record,file=original.silk,file_id=rec-id]",
                            is_group_msg=lambda: False)
    result = await qq.extract_media(SimpleNamespace(get_record=get_record), event)
    assert calls == [{"file_id": "rec-id", "out_format": "wav"}]
    assert len(result) == 1
    assert result[0].media_type == "audio/wav"
    assert result[0].data == converted.read_bytes()


@pytest.mark.asyncio
async def test_transcribed_audio_keeps_images_but_no_audio_snapshots(isolated_config, tmp_path, monkeypatch):
    from io import BytesIO
    from PIL import Image
    from redlotus.TTS import Transcript, asr, audio
    isolated_config["storage"]["runtime_dir"] = "WorkDatabase/runtime"
    store = ReferenceStore(WorkspaceContext.from_path(tmp_path))
    system = SimpleNamespace(toolkit=SimpleNamespace(_references=store))
    class FakeAudioIO:
        @staticmethod
        async def parse_input(source, **kwargs):
            yield object()
    recognized = 0
    class FakeRecognizer:
        def __init__(self, service=None):
            pass
        async def recognize(self, pcm):
            nonlocal recognized
            async for _ in pcm:
                pass
            recognized += 1
            yield Transcript(f"spoken-{recognized}", True)
    monkeypatch.setattr(audio, "AudioIO", FakeAudioIO)
    monkeypatch.setattr(asr, "StreamingRecognizer", FakeRecognizer)
    silk = b"#!SILK_V3 synthetic"
    image = BytesIO()
    Image.new("RGB", (2, 2), "green").save(image, format="PNG")
    message = UserMessage("typed", attachments=[
        BinaryContent(image.getvalue(), media_type="image/png", identifier="first.png"),
        BinaryContent(silk, media_type="audio/silk", identifier="first.silk"),
        BinaryContent(image.getvalue(), media_type="image/png", identifier="second.png"),
        BinaryContent(silk, media_type="audio/silk", identifier="second.silk"),
    ], voice=True)
    await qq.transcribe_voice_message(system, message)
    await store.prepare_message(message)
    assert message.text == "typed\nspoken-1\nspoken-2"
    assert recognized == 2
    assert [ref.name for ref in message.references] == ["first.png", "second.png"]
    assert [ref.source for ref in message.references] == ["attachment:0", "attachment:1"]
    assert all(ref.snapshot.read_bytes() != silk for ref in message.references)
    assert message.attachments == []
    prompt = message.to_prompt()
    assert [ref.name for ref in message.references] == ["first.png", "second.png"]
    assert [sum(marker in part for part in prompt if isinstance(part, str)) for marker in ("spoken-1", "spoken-2")] == [1, 1]


@pytest.mark.asyncio
async def test_cancelled_transcription_closes_its_parser(isolated_config, tmp_path, monkeypatch):
    from redlotus.TTS import asr, audio
    isolated_config["storage"]["runtime_dir"] = "WorkDatabase/runtime"
    store = ReferenceStore(WorkspaceContext.from_path(tmp_path))
    system = SimpleNamespace(toolkit=SimpleNamespace(_references=store))
    entered = asyncio.Event()
    closed = []
    class FakeAudioIO:
        @staticmethod
        async def parse_input(source, **kwargs):
            try:
                yield object()
            finally:
                closed.append("parser")
    class FakeRecognizer:
        def __init__(self, service=None):
            pass
        async def recognize(self, pcm):
            async for _ in pcm:
                entered.set()
                await asyncio.Event().wait()
                yield None
    monkeypatch.setattr(audio, "AudioIO", FakeAudioIO)
    monkeypatch.setattr(asr, "StreamingRecognizer", FakeRecognizer)
    message = UserMessage("", attachments=[BinaryContent(b"#!SILK_V3 synthetic",
        media_type="audio/silk", identifier="voice.silk")], voice=True)
    task = asyncio.create_task(qq.transcribe_voice_message(system, message))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert closed == ["parser"]
