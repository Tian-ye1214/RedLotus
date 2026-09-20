"""Api WeChat responsibilities."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from wechatbot import WeChatBot

import asyncio
from functools import partial

from pydantic_ai import BinaryContent

import redlotus.runtime.resources as _runtime_resources
from redlotus.api.base import AttachmentError, BotBase
from redlotus.documents.interaction import UserMessage


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
        identity = (
            getattr(msg, "file_name", None)
            or getattr(msg, "id", None)
            or getattr(msg, "type", None)
            or "attachment"
        )
        try:
            media = await bot.download(msg)
        except Exception as e:
            raise AttachmentError(str(identity), str(e)) from e
        if media is not None and getattr(media, "data", None):
            filename = getattr(media, "file_name", None) or ""
            mtype = (getattr(media, "type", None) or "").lower()
            mime = self.guess_download_mime(filename=filename, media_type_key=mtype)
            attachments.append(
                BinaryContent(
                    data=media.data, media_type=mime, identifier=filename or None
                )
            )
        elif (getattr(msg, "type", None) or "").lower() in self._MIME_MAP:
            raise AttachmentError(str(identity), "download returned no data")
        return UserMessage(
            text=text,
            attachments=attachments,
            original_text=self.clean_text(msg.text or ""),
        )

    async def _handle_message(self, bot: WeChatBot, msg) -> None:
        if not msg.user_id:
            return
        session_id = f"{self.session_prefix}{msg.user_id}"
        text = self.clean_text(msg.text or "")
        await self.dispatch_event(
            session_id,
            text,
            partial(self._build_user_message, bot, msg),
            partial(bot.reply, msg),
            retry_context=msg,
        )

    async def _async_main(self) -> None:
        from wechatbot import WeChatBot

        self._released = False
        kwargs: dict = {
            "on_qr_url": lambda url: _runtime_resources.info(f"[WeChat] 请扫码登录: {url}"),
            "on_scanned": lambda: _runtime_resources.info("[WeChat] 已扫码，确认登录中..."),
            "on_expired": lambda: _runtime_resources.warning("[WeChat] 登录二维码已过期"),
            "on_error": lambda err: _runtime_resources.error(f"[WeChat] SDK 错误: {err}"),
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
                _runtime_resources.debug("[WeChat] bot.stop 失败: %s", e)

    def run(self) -> None:
        asyncio.run(self._async_main())


if __name__ == "__main__":
    WeChatAgentBot().run()
