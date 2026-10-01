from __future__ import annotations

import asyncio
import base64
import logging
import os
import re
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING

from redlotus.TTS import AudioSegment
from redlotus.api import ChannelSender, OutboundFile
from redlotus.api.base import BotBase, main
from redlotus.api.media import extract_media, iter_segments
from redlotus.runtime.config import user_config_dir
from redlotus.sessions.control import UserMessage

if TYPE_CHECKING:
    from ncatbot.core import BaseMessageEvent


class _TransportDebugFilter(logging.Filter):
    """Keep SDK connection diagnostics without logging raw message frames."""

    def filter(self, record: logging.LogRecord) -> bool:
        return record.levelno > logging.DEBUG


_TRANSPORT_DEBUG_FILTER = _TransportDebugFilter()


class _ConfigLogFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        if type(record.msg).__module__ == "ncatbot.utils.config":
            record.msg, record.args = "QQ SDK 配置已校验，私人字段不写入日志。", ()
        return True


_CONFIG_LOG_FILTER = _ConfigLogFilter()


class _MediaRequest:
    """Omit absent SDK media selectors: NapCat accepts omission, not JSON null."""

    def __init__(self, send):
        self.send = send

    async def __call__(self, path: str, params: dict | None = None, **kwargs):
        if params is not None and path in {"/get_image", "/get_record", "/get_file"}:
            params = {key: value for key, value in params.items()
                if key not in {"file", "file_id"} or value is not None}
        return await self.send(path, params, **kwargs)


class QQBot(BotBase):
    def __init__(self):
        super().__init__()
        # ncatbot freezes its configuration on import. Validate the user's file first.
        config_path = Path(os.environ.get("NCATBOT_CONFIG_PATH") or user_config_dir() / "config.yaml")
        if not config_path.is_file():
            raise ValueError(f"[QQ] 缺少 NapCat 配置文件: {config_path}；请按 api/config.yaml.example 填写。")
        os.environ["NCATBOT_CONFIG_PATH"] = str(config_path)
        from ncatbot.core import BotClient
        for name in ("Adapter", "websockets.client"):
            transport_log = logging.getLogger(name)
            if _TRANSPORT_DEBUG_FILTER not in transport_log.filters:
                transport_log.addFilter(_TRANSPORT_DEBUG_FILTER)
        config_log = logging.getLogger("Config")
        if _CONFIG_LOG_FILTER not in config_log.filters:
            config_log.addFilter(_CONFIG_LOG_FILTER)
        self._doctor()
        self._bot_client = BotClient()
        self._bot_client.api.async_callback = _MediaRequest(self._bot_client.api.async_callback)
        adapter = self._bot_client.adapter
        adapter.connect_websocket = partial(self._run_connection, adapter.connect_websocket)
        self._bot_client.add_private_message_handler(self._handle_message)
        self._bot_client.add_group_message_handler(self._handle_message)

    platform_tag = "QQ"
    session_prefix = "qq_"

    def clean_text(self, raw: str) -> str:
        return re.sub(r"\[CQ:[^\]]+\]", "", raw or "")

    def _is_at_me(self, event: BaseMessageEvent) -> bool:
        from ncatbot.core import GroupMessageEvent
        from ncatbot.utils import config

        if not isinstance(event, GroupMessageEvent):
            return True
        msg = getattr(event, "message", None)
        return msg is not None and msg.is_user_at(config.bt_uin)

    async def _run_connection(self, connect) -> None:
        from redlotus.api.base import start_speech
        try:
            await start_speech()
            await connect()
        finally:
            await self.release_all_resources_async()

    def adapt_message(self, event: BaseMessageEvent):
        raw_text = (event.raw_message or "")
        if not self._is_at_me(event):
            return
        is_group = event.is_group_msg()
        session_id = f"group_{event.group_id}" if is_group else f"private_{event.user_id}"
        user_text = self.clean_text(raw_text)
        kinds = {kind for kind, _ in iter_segments(event)}
        has_voice = "record" in kinds or bool(re.search(r"\[CQ:record(?:,|\])", raw_text))
        has_media = bool(kinds & {"image", "video", "file", "record"}) or bool(
            re.search(r"\[CQ:(?:image|video|file|record)(?:,|\])", raw_text)
        )
        reply = partial(event.reply, at=False) if is_group else partial(event.reply)
        reply.file_sender = QQFileSender(self._bot_client.api, event)
        reply.speech_sender = partial(self.send_voice, event)
        reply.speech_format = "silk"
        return (
            session_id,
            UserMessage(
                text=user_text,
                original_text=user_text,
                voice=has_voice,
            ),
            reply,
            partial(extract_media, self._bot_client.api, event) if has_media else None,
        )

    async def send_voice(self, event: BaseMessageEvent, segment: AudioSegment) -> None:
        if segment.format != "silk":
            raise ValueError("QQ 语音回复需要 SILK 音频")
        payload = "base64://" + base64.b64encode(segment.data).decode("ascii")
        if event.is_group_msg():
            await self._bot_client.api.send_group_record(event.group_id, payload)
        else:
            await self._bot_client.api.send_private_record(event.user_id, payload)

    def _doctor(self) -> None:
        """启动前体检：配置缺失/无效时立即报错退出，避免 ncatbot 回退到 input() 静默卡死。"""
        from ncatbot.utils import config
        from ncatbot.utils.config import strong_password_check

        config_path = Path(os.environ["NCATBOT_CONFIG_PATH"])
        uin = str(config.bt_uin or "")
        if uin in ("", "None", "123456"):
            raise ValueError(
                f"[QQ] 机器人 QQ 号未配置。请在 {config_path} 中填写 bt_uin。"
            )
        token = config.napcat.webui_token
        if config.napcat.enable_webui and not strong_password_check(token):
            raise ValueError(
                f"[QQ] NapCat WebUI 令牌强度不足（{config_path} 的 napcat.webui_token）。"
                f"请改为至少 12 位、含数字与大小写字母及特殊符号的强密码。"
            )

    def run(self, **kwargs):
        from ncatbot.utils import config
        self._doctor()
        self._released = False
        save = config.save
        config.save = self._keep_config
        try:
            self._bot_client.run_frontend(**kwargs)
        finally:
            config.save = save
            asyncio.run(self.release_all_resources_async())

    def _keep_config(self) -> None:
        """SDK persistence hook: the channel only reads the user's NapCat settings."""
        logging.getLogger("Config").debug("QQ 使用现有配置，不自动回写。")


class QQFileSender(ChannelSender):
    def __init__(self, api, event):
        self.api, self.event = api, event

    async def send_file(self, item: OutboundFile) -> str:
        payload = "base64://" + base64.b64encode(item.data).decode("ascii")
        prefix = "group" if self.event.is_group_msg() else "private"
        recipient = self.event.group_id if prefix == "group" else self.event.user_id
        if item.media_type.startswith("image/"):
            await getattr(self.api, f"send_{prefix}_image")(recipient, payload)
        else:
            await getattr(self.api, f"send_{prefix}_file")(recipient, payload, name=item.path.name)
        return f"QQ 已受理文件发送：{item.path.name}；未自动重复发送。"


if __name__ == "__main__":
    main(QQBot)
