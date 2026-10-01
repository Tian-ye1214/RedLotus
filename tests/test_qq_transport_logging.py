"""QQ transport diagnostics must not persist outgoing audio payloads."""

import base64
import logging

import pytest

from redlotus.TTS import AudioSegment
from redlotus.api.QQ import QQBot


@pytest.mark.asyncio
async def test_qq_sdk_transport_debug_cannot_log_short_audio(tmp_path, monkeypatch):
    config = tmp_path / "config.yaml"
    config.write_text("{}", encoding="utf-8")
    monkeypatch.setenv("NCATBOT_CONFIG_PATH", str(config))
    monkeypatch.setenv("LOG_FILE_PATH", str(tmp_path / "ncatbot-logs"))
    monkeypatch.setenv("LOG_FORMAT", "%(message)s")
    monkeypatch.setenv("LOG_FILE_FORMAT", "%(message)s")

    import ncatbot.core
    from ncatbot.core.adapter.adapter import Adapter, LOG
    from ncatbot.core.api.api import BotAPI

    class Client:
        def __init__(self):
            self.adapter = Adapter()
            self.api = BotAPI(self.adapter.send)

        def add_private_message_handler(self, handler):
            pass

        def add_group_message_handler(self, handler):
            pass

    class Capture(logging.Handler):
        def __init__(self):
            super().__init__()
            self.records = []

        def emit(self, record):
            self.records.append(record)

    monkeypatch.setattr(ncatbot.core, "BotClient", Client)
    monkeypatch.setattr(QQBot, "_doctor", lambda self: None)
    websocket = logging.getLogger("websockets.client")
    for logger in (LOG, websocket):
        monkeypatch.setattr(logger, "filters", list(logger.filters))
    bot = QQBot()
    sink = Capture()
    root = logging.getLogger()
    monkeypatch.setattr(root, "handlers", [sink])
    monkeypatch.setattr(root, "level", logging.DEBUG)
    for logger in (LOG, websocket):
        monkeypatch.setattr(logger, "level", logging.DEBUG)
        monkeypatch.setattr(logger, "propagate", True)

    class Event:
        user_id = "synthetic"

        def is_group_msg(self):
            return False

    raw = b"\x02#!SILK_V3tiny"
    assert len(base64.b64encode(raw)) < 1000
    with pytest.raises(ConnectionError, match="WebSocket"):
        await bot.send_voice(Event(), AudioSegment(raw, "silk", 1.0, 24000))
    websocket.debug("raw frame: base64://synthetic")
    LOG.info("NapCat WebSocket 连接成功")
    LOG.error("NapCat WebSocket 连接出错")
    assert not any("base64://" in record.getMessage() for record in sink.records)
    assert any(record.levelno == logging.INFO and "连接成功" in record.getMessage() for record in sink.records)
    assert any(record.levelno == logging.ERROR and "连接出错" in record.getMessage() for record in sink.records)
