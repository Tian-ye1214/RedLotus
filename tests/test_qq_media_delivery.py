"""Exercise QQ media at the real SDK boundary without contacting an account."""
import base64
from types import SimpleNamespace

import pytest

from redlotus.api.QQ import QQBot
from redlotus.api import media
from redlotus.TTS import AudioSegment


@pytest.fixture
def sdk_bot(tmp_path, monkeypatch):
    monkeypatch.setenv("LOG_FORMAT", "%(message)s")
    monkeypatch.setenv("LOG_FILE_FORMAT", "%(message)s")
    monkeypatch.setenv("LOG_FILE_PATH", str(tmp_path / "logs"))
    config = tmp_path / "config.yaml"
    config.write_text("{}", encoding="utf-8")
    monkeypatch.setenv("NCATBOT_CONFIG_PATH", str(config))
    import ncatbot.core
    from ncatbot.core.api.api import BotAPI

    class Client:
        def __init__(self):
            self.calls = []
            self.response = {}
            self.api = BotAPI(self.send)
            self.adapter = SimpleNamespace(connect_websocket=None)

        async def send(self, action, params=None, **kwargs):
            self.calls.append((action, params))
            self.options = kwargs
            # NapCat validates optional strings when present, including JSON null.
            if action in {"/get_image", "/get_record", "/get_file"} and any(
                value is None for key, value in params.items() if key in {"file", "file_id"}
            ):
                return {"status": "failed", "retcode": 1400,
                    "message": "Schema compilation error: Expected string", "data": None}
            return {"status": "ok", "retcode": 0, "data": self.response}

        def add_private_message_handler(self, handler):
            pass

        def add_group_message_handler(self, handler):
            pass

    monkeypatch.setattr(ncatbot.core, "BotClient", Client)
    monkeypatch.setattr(QQBot, "_doctor", lambda self: None)
    return QQBot()


@pytest.mark.asyncio
@pytest.mark.parametrize("group", [False, True])
async def test_voice_uses_standalone_record_with_real_sdk(tmp_path, monkeypatch, group):
    monkeypatch.setenv("LOG_FORMAT", "%(message)s")
    monkeypatch.setenv("LOG_FILE_FORMAT", "%(message)s")
    monkeypatch.setenv("LOG_FILE_PATH", str(tmp_path / "logs"))
    config = tmp_path / "config.yaml"
    config.write_text("{}", encoding="utf-8")
    monkeypatch.setenv("NCATBOT_CONFIG_PATH", str(config))
    from ncatbot.core import GroupMessageEvent, PrivateMessageEvent
    from ncatbot.core.api.api import BotAPI
    from ncatbot.utils.status import status
    calls = []

    async def callback(action, params):
        calls.append((action, params))
        return {"status": "ok", "retcode": 0, "data": {"message_id": 100}}

    api = BotAPI(callback)
    monkeypatch.setattr(status, "global_api", api)
    event_type = GroupMessageEvent if group else PrivateMessageEvent
    event = event_type({"time": 0, "self_id": 99, "post_type": "message",
        "message_type": "group" if group else "private", "sub_type": "normal" if group else "friend",
        "user_id": 42, "group_id": 43, "message_id": 7, "message": [], "sender": {"user_id": 42}})
    bot = QQBot.__new__(QQBot)
    bot._bot_client = SimpleNamespace(api=api)
    raw = b"\x02#!SILK_V3 synthetic"
    await bot.send_voice(event, AudioSegment(raw, "silk", 1.0, 24000))
    assert len(calls) == 1
    action, params = calls[0]
    assert action == ("/send_group_msg" if group else "/send_private_msg")
    assert str(params["group_id" if group else "user_id"]) == ("43" if group else "42")
    assert [item["type"] for item in params["message"]] == ["record"]
    assert base64.b64decode(params["message"][0]["data"]["file"][9:]) == raw


@pytest.mark.asyncio
async def test_qq_image_uses_sdk_resolved_media_under_fake_dns(tmp_path, monkeypatch, isolated_config, sdk_bot):
    raw = b"\xff\xd8\xffsynthetic jpeg"
    cached = tmp_path / "napcat-image.jpg"
    cached.write_bytes(raw)
    client = sdk_bot._bot_client
    client.response = dict(file=str(cached), url="https://multimedia.nt.qq.com.cn/private-query",
        file_size=str(len(raw)), file_name="original.jpg")

    def blocked_download(*args):
        raise ValueError("附件地址无法解析为公网地址")

    monkeypatch.setattr(media, "download_to_binary", blocked_download)
    event = SimpleNamespace(message=[{"type": "image", "data": {
        "file": "opaque-image.jpg", "url": "https://multimedia.nt.qq.com.cn/private-query"}}])
    result = await media.extract_media(client.api, event)
    assert client.calls == [("/get_image", {"file": "opaque-image.jpg"})]
    assert result[0].data == raw
    assert result[0].media_type == "image/jpeg"
    assert result[0].identifier == "opaque-image.jpg"


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["get_image", "get_record"])
@pytest.mark.parametrize("selector", ["file", "file_id"])
async def test_sdk_media_omits_only_absent_selectors(sdk_bot, method, selector):
    client = sdk_bot._bot_client
    client.response = {"file": "resolved-media"}
    await getattr(client.api, method)(**{selector: "opaque-id"})
    expected = {selector: "opaque-id"}
    if method == "get_record":
        expected["out_format"] = "mp3"
    assert client.calls == [("/" + method, expected)]


@pytest.mark.asyncio
async def test_qq_record_reads_original_silk_through_real_sdk(sdk_bot, tmp_path, monkeypatch, isolated_config):
    raw = b"\x02#!SILK_V3 original"
    cached = tmp_path / "original.silk"
    cached.write_bytes(raw)
    client = sdk_bot._bot_client
    client.response = {"file": str(cached), "file_size": str(len(raw)), "file_name": "original.silk"}
    monkeypatch.setattr(media, "download_to_binary", lambda *args: pytest.fail("use authenticated get_file"))
    event = SimpleNamespace(message=[{"type": "record", "data": {
        "file_id": "opaque-id", "file": "original.silk",
        "url": "https://multimedia.nt.qq.com.cn/private-query"}}])
    result = await media.extract_media(client.api, event)
    assert client.calls == [("/get_file", {"file_id": "opaque-id"})]
    assert result[0].data == raw
    assert result[0].media_type == "audio/silk"
    assert result[0].identifier == "original.silk"


@pytest.mark.asyncio
async def test_qq_record_sdk_local_url_is_a_cache_path(tmp_path, monkeypatch, isolated_config, sdk_bot):
    raw = b"#!AMR\noriginal"
    cached = tmp_path / "original.amr"
    cached.write_bytes(raw)
    client = sdk_bot._bot_client
    client.response = {"url": str(cached), "file_name": "original.amr", "file_size": len(raw)}

    def no_download(*args):
        pytest.fail("SDK local URL must not go to the HTTP downloader")

    monkeypatch.setattr(media, "download_to_binary", no_download)
    event = SimpleNamespace(message=[{"type": "record", "data": {"file": "original.amr"}}])
    result = await media.extract_media(client.api, event)
    assert client.calls == [("/get_file", {"file": "original.amr"})]
    assert result[0].data == raw
    assert result[0].media_type == "audio/amr"


@pytest.mark.asyncio
async def test_qq_record_accepts_in_memory_bytes_and_sdk_base64(isolated_config):
    silk = b"\x02#!SILK_V3 direct"
    wav = b"RIFF\x04\x00\x00\x00WAVE"

    async def get_file(file_id, file):
        assert (file_id, file) == (None, "original.wav")
        return {"base64": base64.b64encode(wav).decode(), "file_name": "original.wav",
            "file_size": len(wav)}

    event = SimpleNamespace(message=[
        {"type": "record", "data": {"file": silk, "name": "direct.silk"}},
        {"type": "record", "data": {"file": "original.wav"}},
    ])
    result = await media.extract_media(SimpleNamespace(get_file=get_file), event)
    assert [(item.data, item.media_type) for item in result] == [
        (silk, "audio/silk"), (wav, "audio/wav")]


@pytest.mark.asyncio
async def test_qq_record_base64_at_exact_limit_is_allowed(isolated_config):
    raw = b"#!SILK_V3x"
    isolated_config["input_limits"]["defaults"]["max_file_bytes"] = len(raw)
    event = SimpleNamespace(message=[{"type": "record", "data": {
        "file": "base64://" + base64.b64encode(raw).decode(), "name": "small.silk"}}])
    result = await media.extract_media(None, event)
    assert result[0].data == raw


@pytest.mark.asyncio
async def test_qq_record_rejects_untrusted_message_path(tmp_path, monkeypatch):
    cached = tmp_path / "private.silk"
    cached.write_bytes(b"\x02#!SILK_V3 private")

    async def get_file(file_id, file):
        pytest.fail("Chat-supplied paths must not become authenticated cache requests")

    def no_download(*args):
        raise ValueError("附件地址必须是完整的 http/https URL")

    monkeypatch.setattr(media, "download_to_binary", no_download)
    event = SimpleNamespace(message=[{"type": "record", "data": {"file": str(cached)}}])
    with pytest.raises(ValueError, match="http/https"):
        await media.extract_media(SimpleNamespace(get_file=get_file), event)


@pytest.mark.asyncio
async def test_qq_record_rejects_wrong_sdk_identity(tmp_path, isolated_config):
    cached = tmp_path / "private.silk"
    cached.write_bytes(b"\x02#!SILK_V3 private")

    async def get_file(file_id, file):
        return {"file": str(cached), "file_name": "other.silk"}

    event = SimpleNamespace(message=[{"type": "record", "data": {"file": "wanted.silk"}}])
    with pytest.raises(ValueError, match="身份|文件名"):
        await media.extract_media(SimpleNamespace(get_file=get_file), event)


@pytest.mark.asyncio
async def test_qq_record_requires_sdk_identity_for_name_query(tmp_path, isolated_config):
    cached = tmp_path / "original.silk"
    cached.write_bytes(b"\x02#!SILK_V3 original")

    async def get_file(file_id, file):
        return {"file": str(cached)}

    event = SimpleNamespace(message=[{"type": "record", "data": {"file": "original.silk"}}])
    with pytest.raises(ValueError, match="身份"):
        await media.extract_media(SimpleNamespace(get_file=get_file), event)


@pytest.mark.asyncio
async def test_qq_record_checks_actual_cache_size(tmp_path, isolated_config):
    isolated_config["input_limits"]["defaults"]["max_file_bytes"] = 10
    cached = tmp_path / "large.silk"
    cached.write_bytes(b"\x02#!SILK_V3 too much")

    async def get_file(file_id, file):
        return {"file": str(cached), "file_name": "large.silk", "file_size": 1}

    event = SimpleNamespace(message=[{"type": "record", "data": {"file": "large.silk"}}])
    with pytest.raises(ValueError, match="限额"):
        await media.extract_media(SimpleNamespace(get_file=get_file), event)


@pytest.mark.asyncio
async def test_qq_record_rejects_misreported_sdk_size(tmp_path, isolated_config):
    raw = b"\x02#!SILK_V3 original"
    cached = tmp_path / "original.silk"
    cached.write_bytes(raw)

    async def get_file(file_id, file):
        return {"file": str(cached), "file_name": "original.silk", "file_size": len(raw) - 1}

    event = SimpleNamespace(message=[{"type": "record", "data": {"file": "original.silk"}}])
    with pytest.raises(ValueError, match="大小不一致"):
        await media.extract_media(SimpleNamespace(get_file=get_file), event)


@pytest.mark.asyncio
async def test_qq_record_unreachable_sdk_cache_has_clear_error(monkeypatch, isolated_config):
    async def get_file(file_id, file):
        return {"url": "C:/napcat/missing/original.silk", "file_name": "original.silk"}

    def no_download(*args):
        pytest.fail("SDK paths must not be sent to the public downloader")

    monkeypatch.setattr(media, "download_to_binary", no_download)
    event = SimpleNamespace(message=[{"type": "record", "data": {"file": "original.silk"}}])
    with pytest.raises(ValueError, match="NapCat.*缓存.*不可访问"):
        await media.extract_media(SimpleNamespace(get_file=get_file), event)


@pytest.mark.asyncio
async def test_sdk_compatibility_preserves_other_requests(sdk_bot):
    client = sdk_bot._bot_client
    params = {"value": None, "enabled": False, "count": 0}
    result = await client.api.async_callback("/unrelated", params)
    assert result["status"] == "ok"
    assert client.calls == [("/unrelated", params)]
    assert client.calls[0][1] is params


@pytest.mark.asyncio
async def test_sdk_compatibility_preserves_parameterless_calls_and_timeout(sdk_bot):
    client = sdk_bot._bot_client
    client.response = {"user_id": "42", "nickname": "test"}
    assert (await client.api.get_login_info()).user_id == "42"
    assert client.calls == [("/get_login_info", None)]
    await client.api.async_callback("/get_image", {"file": "opaque", "file_id": None}, timeout=5)
    assert client.options == {"timeout": 5}


@pytest.mark.asyncio
async def test_qq_sdk_cache_obeys_input_limit_before_read(tmp_path, monkeypatch, isolated_config):
    isolated_config["input_limits"]["defaults"]["max_file_bytes"] = 4
    cached = tmp_path / "large.jpg"
    cached.write_bytes(b"\xff\xd8\xfftoo large")

    async def get_image(*, file):
        return {"file": str(cached), "file_size": 1}

    event = SimpleNamespace(message=[{"type": "image", "data": {"file": "opaque.jpg"}}])
    with pytest.raises(ValueError, match="限额"):
        await media.extract_media(SimpleNamespace(get_image=get_image), event)


@pytest.mark.asyncio
async def test_qq_image_rejects_nonimage_sdk_result(tmp_path, isolated_config):
    cached = tmp_path / "not-an-image.jpg"
    cached.write_bytes(b"private text must not be treated as image")

    async def get_image(*, file):
        return {"file": str(cached)}

    event = SimpleNamespace(message=[{"type": "image", "data": {"file": "opaque.jpg"}}])
    with pytest.raises(ValueError, match="图片"):
        await media.extract_media(SimpleNamespace(get_image=get_image), event)


@pytest.mark.asyncio
async def test_message_supplied_local_image_path_is_not_read(tmp_path, monkeypatch):
    cached = tmp_path / "private.jpg"
    cached.write_bytes(b"\xff\xd8\xffprivate image")

    async def get_image(**kwargs):
        pytest.fail("Untrusted message paths must not be resolved through the SDK")

    def blocked_download(*args):
        raise ValueError("附件地址必须是完整的 http/https URL")

    monkeypatch.setattr(media, "download_to_binary", blocked_download)
    event = SimpleNamespace(message=[{"type": "image", "data": {"file": str(cached)}}])
    with pytest.raises(ValueError, match="http/https"):
        await media.extract_media(SimpleNamespace(get_image=get_image), event)


@pytest.mark.asyncio
async def test_qq_url_only_image_keeps_public_address_check_and_single_error(monkeypatch, isolated_config):
    monkeypatch.setattr(media, "_resolve_public_addr", lambda host: [])
    event = SimpleNamespace(message=[{"type": "image", "data": {"url": "https://unused.invalid/image"}}])
    with pytest.raises(ValueError) as failure:
        await media.extract_media(None, event)
    text = str(failure.value)
    assert "公网地址" in text
    assert text.count("准备失败") == 1
    assert text.count("请重新发送完整消息") == 1
