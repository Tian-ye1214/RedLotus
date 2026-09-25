from __future__ import annotations

import asyncio
from dataclasses import replace
from functools import partial
from typing import TYPE_CHECKING

from pydantic_ai import BinaryContent

from redlotus.api.base import BotBase, main
from redlotus.runtime import logging as logger
from redlotus.sessions.control import UserMessage

if TYPE_CHECKING:
    from wechatbot import WeChatBot


class WeChatAgentBot(BotBase):
    _MIME_MAP = {
        "image": "image/jpeg",
        "voice": "audio/mpeg",
        "video": "video/mp4",
        "file": "application/octet-stream",
    }

    platform_tag = "WeChat"
    session_prefix = "wx_"

    async def _download_attachments(self, bot: WeChatBot, msg) -> list:
        """Download every SDK media item; one failed item rejects the entire request."""
        attachments = []
        for kind in self._MIME_MAP:
            for index, item in enumerate(getattr(msg, kind + "s")):
                identity = getattr(item, "file_name", None) or f"{kind}[{index + 1}]"
                single = replace(msg, **{name + "s": [item] if name == kind else [] for name in self._MIME_MAP})
                try:
                    media = await bot.download(single)
                    if media is None or not media.data:
                        raise ValueError("下载结果为空")
                except Exception as exc:
                    raise ValueError(f"附件 {identity} 准备失败：{exc}；请重新发送完整消息。") from exc
                attachments.append(BinaryContent(
                    data=media.data,
                    media_type=self.guess_download_mime(filename=identity, media_type_key=media.type),
                    identifier=identity,
                ))
        return attachments

    async def _handle_message(self, bot: WeChatBot, msg) -> None:
        if not msg.user_id:
            return
        session_id = f"{self.session_prefix}{msg.user_id}"
        text = self.clean_text(msg.text or "")
        await self.dispatch_user_message(
            session_id,
            UserMessage(text=text, original_text=text),
            partial(bot.reply, msg),
            prepare=partial(self._download_attachments, bot, msg) if any(getattr(msg, kind + "s") for kind in self._MIME_MAP) else None,
        )

    async def _async_main(self) -> None:
        from wechatbot import WeChatBot

        self._released = False
        kwargs: dict = {
            "on_qr_url": partial(logger.info, "[WeChat] 请扫码登录: %s"),
            "on_scanned": partial(logger.info, "[WeChat] 已扫码，确认登录中..."),
            "on_expired": partial(logger.warning, "[WeChat] 登录二维码已过期"),
            "on_error": partial(logger.error, "[WeChat] SDK 错误: %s"),
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
    main(WeChatAgentBot)
