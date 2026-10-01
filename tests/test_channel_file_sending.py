import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from redlotus.api import BoundChannelSender, OutboundFile, CommandOutput
from redlotus.api.QQ import QQFileSender
from redlotus.api.WeChat import WeChatFileSender
from redlotus.runtime.resources import bind_context
from redlotus.ui.presentation import OUTPUT_SINK, print_message


@pytest.mark.asyncio
@pytest.mark.parametrize("mime", ["image/png", "text/plain"])
async def test_qq_file_and_image_use_native_message_with_bound_recipient(mime, monkeypatch, tmp_path):
    monkeypatch.setenv("LOG_FORMAT", "%(message)s")
    monkeypatch.setenv("LOG_FILE_FORMAT", "%(message)s")
    monkeypatch.setenv("LOG_FILE_PATH", str(tmp_path / "logs"))
    config = tmp_path / "config.yaml"
    config.write_text("{}", encoding="utf-8")
    monkeypatch.setenv("NCATBOT_CONFIG_PATH", str(config))
    from ncatbot.core.api.api import BotAPI

    calls = []
    async def request(path, params, **kwargs):
        calls.append((path, params))
        return {"status": "ok", "retcode": 0, "data": {"message_id": "sent"}}
    sender = QQFileSender(BotAPI(request), SimpleNamespace(is_group_msg=lambda: False, user_id=123))
    await sender.send_file(OutboundFile(Path("item.png"), b"content", mime))
    path, body = calls[0]
    assert path == "/send_private_msg"
    assert body["user_id"] == 123
    assert len(body["message"]) == 1
    assert body["message"][0]["type"] == ("image" if mime.startswith("image") else "file")
    assert body["message"][0]["data"]["file"].startswith("base64://")


@pytest.mark.asyncio
async def test_wechat_uploads_existing_bytes_once_and_retains_message_context():
    calls, message = [], object()
    async def reply_media(original, data):
        calls.append((original, data))
    sender = WeChatFileSender(SimpleNamespace(reply_media=reply_media), message)
    await sender.send_file(OutboundFile(Path("report.txt"), b"report", "text/plain"))
    assert calls == [(message, {"file": b"report", "file_name": "report.txt"})]


@pytest.mark.asyncio
async def test_expired_sender_cannot_send_or_choose_recipient():
    sender = BoundChannelSender(lambda: pytest.fail("expired turn queried recipient"), lambda: False)
    with pytest.raises(asyncio.CancelledError):
        await sender.send_file(OutboundFile(Path("report.txt"), b"report", "text/plain"))


@pytest.mark.asyncio
async def test_command_output_is_task_local():
    async def render(text):
        output = CommandOutput()
        with bind_context(OUTPUT_SINK, output):
            await asyncio.sleep(0)
            print_message(text)
        return output.text
    assert await asyncio.gather(render("first"), render("second")) == ["first", "second"]


@pytest.mark.asyncio
async def test_wechat_tool_burst_stays_in_logs_without_hiding_confirmation(phone):
    from redlotus.runtime import logging as logger
    from redlotus.sessions.control import UserMessage
    from redlotus.tools import registry

    bot, state, _, replies, _ = phone
    confirmation_sent = asyncio.Event()
    question = "是否修改 permission-check.txt：alpha → beta？"

    async def send(text):
        replies.append(text)
        if text == question:
            confirmation_sent.set()

    def read_chunk(index):
        return f"chunk {index}"

    async def parse_chunk(index):
        return f"parsed {index}"

    logger.ensure_configured()
    records = []
    sink = logger._lg.add(lambda message: records.append(message.record), level="INFO")
    token = bot._agent_ctx.set(("wx_owner", state, send, asyncio.get_running_loop(), state.generation))
    registry.set_user_notify_callback(bot._notify)
    try:
        read, parse = registry.wrap_tools_for_user_notify([read_chunk, parse_chunk])
        for index in range(6):
            assert read(index) == f"chunk {index}"
            assert await parse(index) == f"parsed {index}"
        pending = asyncio.create_task(bot._ask_user(question))
        await asyncio.wait_for(confirmation_sent.wait(), 2)
        answer = UserMessage("yes")
        state.question.set_result(answer)
        assert await pending is answer
        assert replies == [question]
        assert len([record for record in records if "🔧" in record["message"]]) == 12
    finally:
        registry.set_user_notify_callback(None)
        bot._agent_ctx.reset(token)
        logger._lg.remove(sink)
