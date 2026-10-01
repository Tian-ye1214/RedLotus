from datetime import datetime
from types import SimpleNamespace
import asyncio
import base64
import hashlib
import json
import threading

import httpx
import pytest
from wechatbot import WeChatBot
from wechatbot.types import DownloadedMedia, IncomingMessage, VoiceContent

from redlotus.TTS import Transcript
from redlotus.api.WeChat import WeChatAgentBot
from redlotus.api.media import transcribe_voice_message


@pytest.mark.asyncio
async def test_platform_transcript_reaches_agent_once_without_download_or_local_asr(phone, monkeypatch):
    import redlotus.TTS as speech

    bot, state, send, replies, calls = phone
    sdk = WeChatBot()
    message = sdk._parse_message({
        "message_type": 1, "from_user_id": "owner", "create_time_ms": 1,
        "context_token": "synthetic-context", "item_list": [
            {"type": 1, "text_item": {"text": "实际文字"}},
            {"type": 3, "voice_item": {"text": "  微信原生转写  "}},
        ],
    })

    async def reply(message, text):
        await send(text)

    async def download(message):
        pytest.fail("A platform transcript must not download audio")

    def local_asr(*args, **kwargs):
        pytest.fail("A platform transcript must not load local ASR")

    monkeypatch.setattr(sdk, "reply", reply)
    monkeypatch.setattr(sdk, "download", download)
    monkeypatch.setattr(speech, "StreamingRecognizer", local_asr)
    await bot._handle_message(sdk, message)
    await state.queue.join()
    assert len(calls) == 1
    assert calls[0].text == "实际文字\n微信原生转写"
    assert calls[0].original_text == calls[0].text
    assert not calls[0].voice
    assert calls[0].attachments == calls[0].references == []
    assert replies == ["✓ 收到，正在处理…", "final answer"]
    await state.prepare_message(state.agent, calls[0])
    assert calls[0].text.count("微信原生转写") == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("missing", [None, "", " \n\t", 123])
async def test_only_missing_platform_transcripts_use_local_asr_in_voice_order(isolated_config, monkeypatch, missing):
    import redlotus.TTS as speech
    from redlotus.TTS.audio import AudioIO

    voices = [VoiceContent(text=missing), VoiceContent(text="平台二"), VoiceContent(text=missing)]
    message = IncomingMessage("owner", "SDK合并正文", "voice", datetime.now(), voices=voices,
        raw={"item_list": [
            {"type": 1, "text_item": {"text": "原文"}},
            *[{"type": 3, "voice_item": {}} for _ in voices],
        ]})
    downloaded = []
    recognized = []

    async def download(single):
        assert len(single.voices) == 1
        selected = single.voices[0]
        assert selected is not voices[1]
        downloaded.append(selected)
        data = b"\x02#!SILK_V3 " + str(len(downloaded)).encode()
        return DownloadedMedia(data, "voice", format="silk")

    async def decode(data, **kwargs):
        assert kwargs["format"] == "audio/silk"
        yield data

    class Recognizer:
        async def recognize(self, pcm):
            async for data in pcm:
                recognized.append(data)
            yield Transcript(f"本地{len(recognized)}", True)

    monkeypatch.setattr(AudioIO, "parse_input", decode)
    monkeypatch.setattr(speech, "StreamingRecognizer", Recognizer)
    sdk = SimpleNamespace(reply=lambda *args: None, download=download)
    _, prepared, _, prepare = WeChatAgentBot().adapt_message(sdk, message)
    prepared.attachments = await prepare()
    await transcribe_voice_message(None, prepared)
    assert prepared.text == "原文\n本地1\n平台二\n本地2"
    assert len(downloaded) == len(recognized) == 2
    assert downloaded[0] is voices[0] and downloaded[1] is voices[2]
    assert prepared.attachments == []
    assert not prepared.voice


@pytest.mark.asyncio
async def test_native_transcript_does_not_hide_missing_voice_download_failure(isolated_config):
    voices = [VoiceContent(text="平台已有结果"), VoiceContent()]
    message = IncomingMessage("owner", "", "voice", datetime.now(), voices=voices)
    downloaded = []

    async def download(single):
        downloaded.append(single.voices[0])
        raise OSError("synthetic download failure")

    sdk = SimpleNamespace(reply=lambda *args: None, download=download)
    _, _, _, prepare = WeChatAgentBot().adapt_message(sdk, message)
    with pytest.raises(ValueError, match="synthetic download failure"):
        await prepare()
    assert len(downloaded) == 1 and downloaded[0] is voices[1]


class VoiceWire:
    def __init__(self, upload_response):
        self.upload_response = upload_response
        self.requests = []
        self.plaintext = b"\x02#!SILK_V3 synthetic in-memory voice"

    async def handle(self, request):
        self.requests.append(request)
        if request.url.path.endswith("getuploadurl"):
            self.upload_request = json.loads(request.content)
            return httpx.Response(200, json=self.upload_response)
        if request.url.path.endswith("sendmessage"):
            return httpx.Response(200, json={"ret": 0, "message_id": "9007199254740993"})
        from wechatbot.crypto import decrypt_aes_ecb
        assert "Authorization" not in request.headers
        key = bytes.fromhex(self.upload_request["aeskey"])
        assert decrypt_aes_ecb(request.content, key) == self.plaintext
        return httpx.Response(200, headers={"x-encrypted-param": "download-reference"})


@pytest.mark.asyncio
@pytest.mark.parametrize("full_url", ["https://cdn.invalid/voice-upload?ticket=opaque", "", None])
async def test_voice_wire_uses_server_upload_url_and_native_payload_without_audio_files(monkeypatch, full_url):
    import builtins
    import io
    from wechatbot.types import Credentials
    from redlotus.TTS import AudioSegment
    from redlotus.api.WeChat import WeChatVoiceSender

    def no_write(original):
        def checked(file, mode="r", *args, **kwargs):
            assert not any(flag in mode for flag in "wax+"), "Voice delivery must not write files"
            return original(file, mode, *args, **kwargs)
        return checked

    monkeypatch.setattr(builtins, "open", no_write(builtins.open))
    monkeypatch.setattr(io, "open", no_write(io.open))
    wire = VoiceWire({"upload_full_url": full_url, "upload_param": "opaque+/="})
    credentials = Credentials("synthetic-token", "https://api.invalid", "account", "owner")
    segment = AudioSegment(wire.plaintext, "silk", 1.25, 24000)
    async with httpx.AsyncClient(transport=httpx.MockTransport(wire.handle)) as client:
        receipt = await WeChatVoiceSender(client, credentials).send("recipient", "fresh-context", segment)
    assert client.is_closed
    assert receipt == "9007199254740993"
    assert len(wire.requests) == 3
    upload, cdn, sent = wire.requests
    if full_url:
        assert str(cdn.url) == full_url
    else:
        assert cdn.url.host == "novac2c.cdn.weixin.qq.com"
        assert cdn.url.params["encrypted_query_param"] == "opaque+/="
    body = json.loads(upload.content)
    assert body["media_type"] == 4 and body["to_user_id"] == "recipient"
    assert body["rawsize"] == len(wire.plaintext)
    assert body["rawfilemd5"] == hashlib.md5(wire.plaintext).hexdigest()
    assert body["filesize"] == len(cdn.content)
    assert upload.headers["Authorization"] == sent.headers["Authorization"] == "Bearer synthetic-token"
    assert upload.headers["iLink-App-Id"] == "bot"
    message = json.loads(sent.content)["msg"]
    assert message["from_user_id"] == ""
    assert message["to_user_id"] == "recipient"
    assert message["context_token"] == "fresh-context"
    assert message["message_type"] == message["message_state"] == 2
    assert message["item_list"] == [{"type": 3, "voice_item": {
        "media": {"encrypt_query_param": "download-reference", "encrypt_type": 1,
                  "aes_key": base64.b64encode(body["aeskey"].encode()).decode()},
        "encode_type": 6, "sample_rate": 24000, "bits_per_sample": 16, "playtime": 1250,
    }}]


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["api", "cdn", "send"])
async def test_voice_delivery_rejection_or_unknown_result_never_retries(stage):
    from wechatbot.types import Credentials
    from redlotus.TTS import AudioSegment
    from redlotus.api.WeChat import WeChatVoiceSender

    wire = VoiceWire({"upload_full_url": "https://cdn.invalid/upload?secret=do-not-log"})

    async def fail(request):
        response = await wire.handle(request)
        count = len(wire.requests)
        if stage == "api" and count == 1:
            return httpx.Response(200, json={"ret": -2, "errmsg": "synthetic rejected"})
        if stage == "cdn" and count == 2:
            return httpx.Response(503)
        if stage == "send" and count == 3:
            raise httpx.ReadTimeout("delivery unknown", request=request)
        return response

    credentials = Credentials("synthetic-token", "https://api.invalid", "account", "owner")
    async with httpx.AsyncClient(transport=httpx.MockTransport(fail)) as client:
        with pytest.raises(Exception) as error:
            await WeChatVoiceSender(client, credentials).send(
                "recipient", "fresh-context", AudioSegment(wire.plaintext, "silk", 1, 24000))
    assert len(wire.requests) == {"api": 1, "cdn": 2, "send": 3}[stage]
    assert "do-not-log" not in str(error.value)
    assert client.is_closed


@pytest.mark.asyncio
async def test_cancel_waits_for_encryption_thread_without_uploading(monkeypatch):
    from wechatbot.types import Credentials
    from redlotus.TTS import AudioSegment
    from redlotus.api.WeChat import EncryptedVoice, WeChatVoiceSender

    started, finish = threading.Event(), threading.Event()
    original = EncryptedVoice.from_bytes

    def encrypt(data):
        started.set()
        assert finish.wait(5)
        return original(data)

    monkeypatch.setattr(EncryptedVoice, "from_bytes", encrypt)
    wire = VoiceWire({"upload_param": "unused"})
    credentials = Credentials("synthetic-token", "https://api.invalid", "account", "owner")
    async with httpx.AsyncClient(transport=httpx.MockTransport(wire.handle)) as client:
        task = asyncio.create_task(WeChatVoiceSender(client, credentials).send(
            "recipient", "fresh-context", AudioSegment(wire.plaintext, "silk", 1, 24000)))
        try:
            assert await asyncio.to_thread(started.wait, 2)
            task.cancel()
            await asyncio.sleep(0)
            assert not task.done()
            task.cancel()
            await asyncio.sleep(0)
            assert not task.done()
        finally:
            finish.set()
            with pytest.raises(asyncio.CancelledError):
                await task
    assert wire.requests == []
    assert client.is_closed
