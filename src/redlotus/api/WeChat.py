from __future__ import annotations

import asyncio
import base64
import hashlib
import secrets
from dataclasses import dataclass, replace
from functools import partial
from typing import TYPE_CHECKING

import httpx
from pydantic_ai import BinaryContent

from redlotus.TTS import AudioSegment, Transcript
from redlotus.api import ChannelSender, OutboundFile
from redlotus.api.base import BotBase, main
from redlotus.api.media import mime_magic
from redlotus.runtime import logging as logger
from redlotus.runtime.config import config_value, settings
from redlotus.runtime.resources import thread_work
from redlotus.sessions.control import UserMessage

if TYPE_CHECKING:
    from wechatbot import WeChatBot
    from wechatbot.types import CDNMedia, Credentials


@dataclass(frozen=True)
class EncryptedVoice:
    data: bytes
    key: str
    filekey: str
    raw_size: int
    raw_md5: str

    @classmethod
    def from_bytes(cls, data: bytes) -> EncryptedVoice:
        """Prepare the complete CDN payload in the worker thread."""
        from wechatbot.crypto import encrypt_aes_ecb

        key = secrets.token_bytes(16)
        return cls(encrypt_aes_ecb(data, key), key.hex(), secrets.token_hex(16),
                   len(data), hashlib.md5(data).hexdigest())


class WeChatVoiceSender:
    """Send native voice independently of the SDK's private media transport."""

    def __init__(self, client: httpx.AsyncClient, credentials: Credentials):
        if credentials is None:
            raise ValueError("微信尚未登录")
        self.client = client
        self.credentials = credentials

    async def _request(self, endpoint: str, payload: dict[str, object]) -> dict[str, object]:
        from wechatbot.errors import ApiError
        from wechatbot.protocol import CHANNEL_VERSION, DEFAULT_BOT_AGENT, ILINK_APP_CLIENT_VERSION, ILINK_APP_ID

        response = await self.client.post(
            f"{self.credentials.base_url.rstrip('/')}/ilink/bot/{endpoint}",
            headers={
                "AuthorizationType": "ilink_bot_token", "Authorization": f"Bearer {self.credentials.token}",
                "X-WECHAT-UIN": base64.b64encode(str(secrets.randbits(32)).encode()).decode(),
                "iLink-App-Id": ILINK_APP_ID, "iLink-App-ClientVersion": ILINK_APP_CLIENT_VERSION,
            },
            json={**payload, "base_info": {"channel_version": CHANNEL_VERSION, "bot_agent": DEFAULT_BOT_AGENT}},
        )
        response.raise_for_status()
        result = response.json()
        if not isinstance(result, dict):
            raise ValueError("微信语音接口返回内容无效")
        if code := result.get("ret", 0) or result.get("errcode", 0):
            raise ApiError(result.get("errmsg") or "微信语音请求被拒绝", errcode=code)
        return result

    async def upload(self, recipient: str, data: bytes) -> CDNMedia:
        from wechatbot.protocol import CDN_BASE_URL
        from wechatbot.types import CDNMedia, MediaType

        encrypted = await thread_work(EncryptedVoice.from_bytes, data)
        result = await self._request("getuploadurl", {
            "filekey": encrypted.filekey, "media_type": int(MediaType.VOICE), "to_user_id": recipient,
            "rawsize": encrypted.raw_size, "rawfilemd5": encrypted.raw_md5,
            "filesize": len(encrypted.data), "no_need_thumb": True, "aeskey": encrypted.key,
        })
        full_url = result.get("upload_full_url")
        if isinstance(full_url, str) and full_url.strip():
            url = httpx.URL(full_url.strip())
            if url.scheme != "https":
                raise ValueError("微信语音上传地址必须使用 HTTPS")
        elif isinstance(result.get("upload_param"), str) and result["upload_param"]:
            url = httpx.URL(f"{CDN_BASE_URL}/upload", params={
                "encrypted_query_param": result["upload_param"], "filekey": encrypted.filekey,
            })
        else:
            raise ValueError("微信未返回有效的语音上传地址")
        try:
            response = await self.client.post(url, content=encrypted.data,
                                              headers={"Content-Type": "application/octet-stream"})
        except httpx.RequestError as exc:
            raise ValueError(f"微信语音上传未确认（{type(exc).__name__}），未重试") from None
        if response.status_code != 200:
            raise ValueError(f"微信语音上传失败（HTTP {response.status_code}）")
        download = response.headers.get("x-encrypted-param", "").strip()
        if not download:
            raise ValueError("微信语音上传未返回媒体引用")
        return CDNMedia(download, base64.b64encode(encrypted.key.encode()).decode(), 1)

    async def send(self, recipient: str, context: str, segment: AudioSegment) -> str | None:
        """Return an acceptance ID, never a client delivery confirmation."""
        from wechatbot.types import MessageItemType, MessageState, MessageType

        if not context or not recipient:
            raise ValueError("微信语音缺少接收人或原消息会话令牌")
        if segment.format != "silk" or not segment.data.startswith(b"\x02#!SILK_V3") or not 0 < segment.duration <= 55:
            raise ValueError("微信语音需要不超过 55 秒的腾讯 SILK 音频")
        media = await self.upload(recipient, segment.data)
        result = await self._request("sendmessage", {"msg": {
            "from_user_id": "", "to_user_id": recipient, "client_id": secrets.token_hex(16),
            "message_type": int(MessageType.BOT), "message_state": int(MessageState.FINISH),
            "context_token": context, "item_list": [{"type": int(MessageItemType.VOICE), "voice_item": {
                "media": {"encrypt_query_param": media.encrypt_query_param,
                          "aes_key": media.aes_key, "encrypt_type": media.encrypt_type},
                "encode_type": 6, "playtime": round(segment.duration * 1000),
                "sample_rate": segment.sample_rate, "bits_per_sample": 16,
            }}],
        }})
        identity = result.get("message_id")
        return str(identity) if identity is not None else None


class WeChatAgentBot(BotBase):
    _MIME_MAP = {
        "image": "image/jpeg",
        "voice": "audio/silk",
        "video": "video/mp4",
        "file": "application/octet-stream",
    }

    platform_tag = "WeChat"
    session_prefix = "wx_"

    def _notify(self, text):
        """Registry already logs tool progress; avoid an iLink send for every tool."""
        return

    async def _download_attachments(self, bot: WeChatBot, msg) -> list[BinaryContent | Transcript]:
        """Use platform voice transcripts and download only unresolved media."""
        attachments = []
        remaining = {kind: list(getattr(msg, kind + "s")) for kind in self._MIME_MAP}
        kinds = {2: "image", 3: "voice", 4: "file", 5: "video"}
        order = [kinds[item["type"]] for item in (msg.raw or {}).get("item_list", []) if item.get("type") in kinds]
        order.extend(kind for kind, items in remaining.items() for _ in range(max(0, len(items) - order.count(kind))))
        for index, kind in enumerate(order):
            if remaining[kind]:
                item = remaining[kind].pop(0)
                transcript = getattr(item, "text", None) if kind == "voice" else None
                if isinstance(transcript, str) and transcript.strip():
                    attachments.append(Transcript(transcript.strip(), True, "wechat-native-asr"))
                    continue
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
        reply.file_sender = WeChatFileSender(bot, msg)
        try:
            if not self._silk_verified(bot, msg.user_id):
                raise ValueError("当前微信账号与接收人尚未验证原生语音播放，不会改发音频附件")
        except ValueError as exc:
            reply.speech_error = exc
        reply.speech_sender = partial(self.send_voice, bot, msg)
        reply.speech_format = "silk"
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

    async def send_voice(self, bot: WeChatBot, msg, segment: AudioSegment) -> None:
        if segment.format != "silk" or not self._silk_verified(bot, msg.user_id):
            raise ValueError("该微信账号与接收人未启用 SILK 原生语音")
        if not msg._context_token:
            raise ValueError("微信原生语音缺少原消息会话令牌")
        async with httpx.AsyncClient() as client:
            await WeChatVoiceSender(client, bot.get_credentials()).send(msg.user_id, msg._context_token, segment)

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


class WeChatFileSender(ChannelSender):
    def __init__(self, bot, message):
        self.bot, self.message = bot, message

    async def send_file(self, item: OutboundFile) -> str:
        content = ({"image": item.data} if item.media_type.startswith("image/")
                   else {"file": item.data, "file_name": item.path.name})
        await self.bot.reply_media(self.message, content)
        return f"微信已受理文件发送：{item.path.name}；未自动重复发送。"


if __name__ == "__main__":
    main(WeChatAgentBot)
