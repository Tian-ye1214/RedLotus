from __future__ import annotations

import asyncio
import os
import re
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING

from redlotus.api.base import BotBase, main
from redlotus.api.qq_media_helpers import extract_media, iter_segments
from redlotus.runtime.config import get_env, user_config_dir
from redlotus.sessions.control import UserMessage

if TYPE_CHECKING:
    from ncatbot.core import BaseMessageEvent


class QQBot(BotBase):
    _ENV_AGENT_TIMEOUT = "QQ_AGENT_TIMEOUT_S"
    _ENV_SEND_TIMEOUT = "QQ_SEND_REPLY_TIMEOUT_S"
    _FILE_ALLOW_EXT = frozenset(
        {
            ".png",
            ".jpg",
            ".jpeg",
            ".gif",
            ".webp",
            ".bmp",
            ".mp4",
            ".mov",
            ".mkv",
            ".webm",
            ".pdf",
            ".txt",
            ".md",
            ".docx",
            ".doc",
            ".xlsx",
            ".xls",
            ".pptx",
            ".ppt",
            ".csv",
            ".json",
            ".html",
        }
    )

    def __init__(self):
        super().__init__()
        # ncatbot freezes its configuration on import. Validate the user's file first.
        config_path = Path(os.environ.get("NCATBOT_CONFIG_PATH") or user_config_dir() / "config.yaml")
        if not config_path.is_file():
            raise ValueError(f"[QQ] 缺少 NapCat 配置文件: {config_path}；请按 api/config.yaml.example 填写。")
        os.environ["NCATBOT_CONFIG_PATH"] = str(config_path)
        from ncatbot.core import BotClient
        from ncatbot.utils import config

        if uin := get_env("QQBOT_ID", warn=False):
            config.set_bot_uin(uin)
        self._doctor()
        self._bot_client = BotClient()
        adapter = self._bot_client.adapter
        adapter.connect_websocket = partial(self._run_connection, adapter.connect_websocket)
        self._bot_client.add_private_message_handler(self._handle_message)
        self._bot_client.add_group_message_handler(self._handle_message)

    platform_tag = "QQ"
    session_prefix = "qq_"

    def clean_text(self, raw: str) -> str:
        return re.sub(r"\[CQ:[^\]]+\]", "", raw or "").strip()

    def _is_at_me(self, event: BaseMessageEvent) -> bool:
        from ncatbot.core import GroupMessageEvent
        from ncatbot.utils import config

        if not isinstance(event, GroupMessageEvent):
            return True
        msg = getattr(event, "message", None)
        return msg is not None and msg.is_user_at(config.bt_uin)

    async def _run_connection(self, connect) -> None:
        try:
            await connect()
        finally:
            await self.release_all_resources_async()

    async def _handle_message(self, event: BaseMessageEvent) -> None:
        raw_text = (event.raw_message or "").strip()
        if not self._is_at_me(event):
            return
        is_group = event.is_group_msg()
        session_id = f"group_{event.group_id}" if is_group else f"private_{event.user_id}"
        user_text = self.clean_text(raw_text)
        await self.dispatch_user_message(
            session_id,
            UserMessage(
                text=user_text,
                original_text=user_text,
            ),
            partial(event.reply, at=False) if is_group else event.reply,
            prepare=partial(extract_media, self._bot_client.api, event, self._FILE_ALLOW_EXT) if (
                any(kind in ("image", "video", "file") for kind, _ in iter_segments(event))
                or re.search(r"\[CQ:(?:image|video|file),", raw_text)
            ) else None,
        )

    def _doctor(self) -> None:
        """启动前体检：配置缺失/无效时立即报错退出，避免 ncatbot 回退到 input() 静默卡死。"""
        from ncatbot.utils import config
        from ncatbot.utils.config import strong_password_check

        config_path = Path(os.environ["NCATBOT_CONFIG_PATH"])
        uin = str(config.bt_uin or "")
        if uin in ("", "None", "123456"):
            raise ValueError(
                "[QQ] 机器人 QQ 号未配置。请设置环境变量 QQBOT_ID，"
                f"或在 {config_path} 中填写 bt_uin。"
            )
        token = config.napcat.webui_token
        if config.napcat.enable_webui and not strong_password_check(token):
            raise ValueError(
                f"[QQ] NapCat WebUI 令牌强度不足（{config_path} 的 napcat.webui_token）。"
                f"请改为至少 12 位、含数字与大小写字母及特殊符号的强密码。"
            )

    def run(self, **kwargs):
        self._doctor()
        self._released = False
        try:
            self._bot_client.run_frontend(**kwargs)
        finally:
            asyncio.run(self.release_all_resources_async())


if __name__ == "__main__":
    main(QQBot)
