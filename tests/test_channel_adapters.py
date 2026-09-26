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
