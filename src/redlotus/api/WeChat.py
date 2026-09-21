from __future__ import annotations

import asyncio
from functools import partial
from typing import TYPE_CHECKING

from pydantic_ai import BinaryContent

from redlotus.runtime import logging as logger
from redlotus.tools.interaction import UserMessage
from redlotus.api.base import BotBase

if TYPE_CHECKING:
    from wechatbot import WeChatBot


class WeChatAgentBot(BotBase):
    _ENV_AGENT_TIMEOUT = "WECHAT_AGENT_TIMEOUT_S"
    _ENV_SEND_TIMEOUT = "WECHAT_SEND_REPLY_TIMEOUT_S"
    _MIME_MAP = {
        "image": "image/jpeg",
        "voice": "audio/mpeg",
        "video": "video/mp4",
        "file": "application/octet-stream",
    }

    platform_tag = "WeChat"
    session_prefix = "wx_"

    async def _build_user_message(self, bot: WeChatBot, msg) -> UserMessage:
        """Build a UserMessage from text plus downloaded media bytes."""
        text = self.clean_text(msg.text or "")
        attachments: list = []
        try:
            media = await bot.download(msg)
        except Exception as e:
            logger.warning(f"[WeChat] 下载媒体失败: {e}")
            media = None
        if media is not None and getattr(media, "data", None):
            filename = getattr(media, "file_name", None) or ""
            mtype = (getattr(media, "type", None) or "").lower()
            mime = self.guess_download_mime(filename=filename, media_type_key=mtype)
            attachments.append(
                BinaryContent(
                    data=media.data, media_type=mime, identifier=filename or None
                )
            )
        return UserMessage(
            text=text,
            attachments=attachments,
            original_text=self.clean_text(msg.text or ""),
        )

    async def _handle_message(self, bot: WeChatBot, msg) -> None:
        if not msg.user_id:
            return
        session_id = f"{self.session_prefix}{msg.user_id}"
        user_message = await self._build_user_message(bot, msg)
        await self.dispatch_user_message(
            session_id,
            user_message,
            partial(bot.reply, msg),
        )

    async def _async_main(self) -> None:
        from wechatbot import WeChatBot

        self._released = False
        kwargs: dict = {
            "on_qr_url": lambda url: logger.info(f"[WeChat] 请扫码登录: {url}"),
            "on_scanned": lambda: logger.info("[WeChat] 已扫码，确认登录中..."),
            "on_expired": lambda: logger.warning("[WeChat] 登录二维码已过期"),
            "on_error": lambda err: logger.error(f"[WeChat] SDK 错误: {err}"),
        }

        bot = WeChatBot(**kwargs)
        await bot.login()
        bot.on_message(partial(self._handle_message, bot))
        try:
            await bot.start()
        finally:
            await self.release_all_resources_async()
            try:
                bot.stop()
            except Exception as e:
                logger.debug("[WeChat] bot.stop 失败: %s", e)

    def run(self) -> None:
        asyncio.run(self._async_main())


if __name__ == "__main__":
    WeChatAgentBot().run()
