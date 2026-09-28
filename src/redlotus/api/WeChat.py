from __future__ import annotations

import asyncio
from dataclasses import replace
from functools import partial
from typing import TYPE_CHECKING
from uuid import uuid4

from pydantic_ai import BinaryContent

from redlotus.TTS import AudioSegment
from redlotus.api.base import BotBase, main
from redlotus.api.media import mime_magic
from redlotus.runtime import logging as logger
from redlotus.runtime.config import config_value, settings
from redlotus.sessions.control import UserMessage

if TYPE_CHECKING:
    from wechatbot import WeChatBot


class WeChatAgentBot(BotBase):
    _MIME_MAP = {
        "image": "image/jpeg",
        "voice": "audio/silk",
        "video": "video/mp4",
        "file": "application/octet-stream",
    }

    platform_tag = "WeChat"
    session_prefix = "wx_"

    async def _download_attachments(self, bot: WeChatBot, msg) -> list:
        """Download every SDK media item; one failed item rejects the entire request."""
        attachments = []
        remaining = {kind: list(getattr(msg, kind + "s")) for kind in self._MIME_MAP}
        kinds = {2: "image", 3: "voice", 4: "file", 5: "video"}
        order = [kinds[item["type"]] for item in (msg.raw or {}).get("item_list", []) if item.get("type") in kinds]
        order.extend(kind for kind, items in remaining.items() for _ in range(max(0, len(items) - order.count(kind))))
        for index, kind in enumerate(order):
            if remaining[kind]:
                item = remaining[kind].pop(0)
                identity = getattr(item, "file_name", None) or f"{kind}[{index + 1}]"
                single = replace(msg, **{name + "s": [item] if name == kind else [] for name in self._MIME_MAP})
                try:
                    media = await bot.download(single)
                    if media is None or not media.data:
                        raise ValueError("下载结果为空")
                except Exception as exc:
                    raise ValueError(f"附件 {identity} 准备失败：{exc}；请重新发送完整消息。") from exc
                identity = media.file_name or identity
                attachments.append(BinaryContent(
                    data=media.data,
                    media_type=mime_magic(media.data) or self.guess_download_mime(filename=identity, media_type_key=media.type),
                    identifier=identity,
                ))
        return attachments

    def adapt_message(self, bot: WeChatBot, msg):
        if not msg.user_id:
            return
        session_id = f"{self.session_prefix}{msg.user_id}"
        text = self.clean_text(msg.text or "")
        if msg.voices:
            text = "\n".join(item.get("text_item", {}).get("text", "")
                             for item in (msg.raw or {}).get("item_list", []) if item.get("type") == 1)
        reply = partial(bot.reply, msg)
        try:
            native = self._silk_verified(bot, msg.user_id)
        except ValueError as exc:
            native, reply.speech_error = False, exc
        reply.speech_sender = partial(self.send_voice, bot, msg, native)
        reply.speech_format = "silk" if native else "wav"
        return (
            session_id,
            UserMessage(text=text, original_text=text, voice=bool(msg.voices)),
            reply,
            partial(self._download_attachments, bot, msg) if any(getattr(msg, kind + "s") for kind in self._MIME_MAP) else None,
        )

    def _silk_verified(self, bot: WeChatBot, recipient: str) -> bool:
        targets = config_value(settings(), ("speech", "wechat_silk_verified_targets"), [], kind=list)
        if any(not isinstance(target, str) for target in targets):
            raise ValueError("speech.wechat_silk_verified_targets 必须是字符串数组")
        credentials = bot.get_credentials() if hasattr(bot, "get_credentials") else None
        account_id = getattr(credentials, "account_id", "")
        return bool(account_id and f"{account_id}:{recipient}" in targets)

    async def send_voice(self, bot: WeChatBot, msg, native: bool, segment: AudioSegment) -> None:
        if native:
            if segment.format != "silk" or not self._silk_verified(bot, msg.user_id):
                raise ValueError("该微信账号与接收人未启用 SILK 原生语音")
            if not msg._context_token:
                raise ValueError("微信原生语音缺少原消息会话令牌")
            send_media = getattr(bot, "_send_media_buffer", None)
            if not callable(send_media):
                raise RuntimeError("当前 wechatbot-sdk 不支持原生语音上传发送流程")
            from wechatbot.client import _cdn_media_dict
            from wechatbot.types import MediaType, MessageItemType

            def build_item(upload):
                return {"type": int(MessageItemType.VOICE), "voice_item": {
                    "media": _cdn_media_dict(upload.media),
                    "encode_type": 6,
                    "playtime": round(segment.duration * 1000),
                    "sample_rate": segment.sample_rate,
                    "bits_per_sample": 16,
                }}
            await send_media(msg.user_id, msg._context_token, segment.data, MediaType.VOICE, build_item)
        else:
            if segment.format != "wav":
                raise ValueError("微信附件语音回复需要 WAV 音频")
            await bot.reply_media(msg, {"file": segment.data, "file_name": f"voice-{uuid4().hex}.wav"})

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
        from redlotus.api.base import start_speech
        try:
            await bot.login()
            bot.on_message(partial(self._handle_message, bot))
            await start_speech()
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
