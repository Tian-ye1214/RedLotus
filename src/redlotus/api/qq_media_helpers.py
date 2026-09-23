"""QQ media preparation: validate and download every attachment before execution."""

from __future__ import annotations

import asyncio
import base64
import html
import ipaddress
import mimetypes
import os
import re
import socket
from functools import partial
from typing import TYPE_CHECKING, Any

import httpx
from pydantic_ai import BinaryContent

from redlotus.runtime.config import settings
from redlotus.runtime.network import ModelInputPolicy

if TYPE_CHECKING:
    from ncatbot.core import BaseMessageEvent


def norm_url(url: str) -> str:
    return html.unescape((url or "").strip())


def mime_magic(raw: bytes) -> str:
    if len(raw) >= 3 and raw[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if len(raw) >= 4 and raw[:4] == b"\x89PNG":
        return "image/png"
    if len(raw) >= 6 and raw[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if len(raw) >= 12 and raw[:4] == b"RIFF" and raw[8:12] == b"WEBP":
        return "image/webp"
    if len(raw) >= 2 and raw[:2] == b"BM":
        return "image/bmp"
    if len(raw) >= 12 and raw[4:8] == b"ftyp":
        return "video/mp4"
    return ""


def pick_ct(url: str, header_ct: str, raw: bytes, filename: str = "") -> str:
    ct = (header_ct or "").split(";")[0].strip().lower()
    if ct and ct not in ("application/octet-stream", "binary/octet-stream"):
        return ct
    for guess in (
        mimetypes.guess_type(filename or "")[0],
        mimetypes.guess_type(url or "")[0],
    ):
        if guess:
            return guess
    return mime_magic(raw) or "application/octet-stream"


def _resolve_public_addr(host: str) -> list[str]:
    """Return every resolved address in order only when all are public.

    Dial these same addresses so connection attempts cannot resolve the host again.
    """
    addresses = list(dict.fromkeys(info[4][0] for info in socket.getaddrinfo(host, None)))
    for addr in addresses:
        ip = ipaddress.ip_address(addr)
        if (
            not ip.is_global
            or ip.is_private
            or ip.is_loopback
            or ip.is_link_local
            or ip.is_reserved
            or ip.is_multicast
        ):
            return []
    return addresses


def download_to_binary(url: str, filename: str = "") -> BinaryContent:
    url = norm_url(url)
    try:
        policy = ModelInputPolicy.for_role()
        for _ in range(settings()["input_limits"]["max_redirects"] + 1):
            parsed = httpx.URL(url)
            if parsed.scheme not in ("http", "https") or not parsed.host:
                raise ValueError("附件地址必须是完整的 http/https URL")
            addresses = _resolve_public_addr(parsed.host)
            if not addresses:
                raise ValueError("附件地址无法解析为公网地址")
            context = httpx.create_ssl_context()

            def wrap_tls(method, *args, **kwargs):
                # HTTP CONNECT currently ignores sni_hostname; keep native verification.
                return method(*args, **(kwargs | {"server_hostname": parsed.host}))

            for method in ("wrap_socket", "wrap_bio"):
                setattr(context, method, partial(wrap_tls, getattr(context, method)))
            with httpx.Client(verify=context, timeout=policy.reference_download_timeout_seconds, follow_redirects=False) as client:
                for ip in addresses:
                    try:
                        with client.stream(
                            "GET", parsed.copy_with(host=ip), headers={"Host": parsed.netloc.decode("ascii")},
                            extensions={"sni_hostname": parsed.host},
                        ) as resp:
                            location = resp.headers.get("location")
                            if resp.is_redirect and location:
                                url = str(parsed.join(location))
                                break
                            resp.raise_for_status()
                            if length := resp.headers.get("content-length"):
                                policy.check([int(length)])
                            chunks, size = [], 0
                            for chunk in resp.iter_bytes():
                                size += len(chunk)
                                policy.check([size])
                                chunks.append(chunk)
                            raw = b"".join(chunks)
                            if not raw:
                                raise ValueError("下载结果为空")
                            return BinaryContent(
                                data=raw,
                                media_type=pick_ct(url, resp.headers.get("content-type", ""), raw, filename=filename),
                                identifier=filename or None,
                            )
                    except (httpx.ConnectError, httpx.ConnectTimeout, httpx.ProxyError):
                        if ip == addresses[-1]:
                            raise
        raise ValueError("重定向次数过多")
    except (httpx.HTTPError, OSError, ValueError) as exc:
        raise ValueError(f"附件 {filename or '媒体'} 准备失败：{exc}；请重新发送完整消息。") from exc


def binary_b64(file_val: str) -> BinaryContent | None:
    if not file_val or not file_val.startswith("base64://"):
        return None
    raw = base64.b64decode(file_val[9:], validate=True)
    if not raw:
        raise ValueError("base64 图片附件为空")
    return BinaryContent(data=raw, media_type=mime_magic(raw) or "image/png")


def iter_segments(event: BaseMessageEvent):
    for seg in getattr(event, "message", None) or []:
        if isinstance(seg, dict):
            yield seg.get("type", ""), seg.get("data", {})
        else:
            yield seg.msg_seg_type, vars(seg)


def extract_image_video(event: BaseMessageEvent) -> list[Any]:
    media = [(kind, data) for kind, data in iter_segments(event) if kind in ("image", "video")]
    if not media:
        media = [(match[1], {"url": match[2]}) for match in re.finditer(
            r"\[CQ:(image|video),[^\]]*url=([^\],]+)", getattr(event, "raw_message", "") or ""
        )]
    attachments = []
    for index, (seg_type, seg_data) in enumerate(media):
        fv = seg_data.get("file") or ""
        bc = binary_b64(fv)
        if bc:
            attachments.append(bc)
            continue
        u = norm_url(seg_data.get("url") or fv)
        attachments.append(download_to_binary(u, f"{seg_type}[{index + 1}]"))
    return attachments


async def file_id_to_binary(
    bot_api, event: BaseMessageEvent, file_id: str, filename: str, allow: frozenset[str]
) -> BinaryContent:
    ext = os.path.splitext(filename or "")[1].lower()
    if not file_id or ext not in allow:
        raise ValueError(f"附件 {filename}（{file_id}）缺少文件 ID 或类型不受支持；请重新发送完整消息。")
    from ncatbot.core import GroupMessageEvent
    try:
        url = await (
            bot_api.get_group_file_url(event.group_id, file_id)
            if isinstance(event, GroupMessageEvent)
            else bot_api.get_private_file_url(file_id)
        )
    except Exception as exc:
        raise ValueError(f"附件 {filename}（{file_id}）无法获取下载地址：{exc}") from exc
    if not url:
        raise ValueError(f"附件 {filename}（{file_id}）下载地址为空")
    return await asyncio.to_thread(download_to_binary, url, filename)


async def extract_media(
    bot_api, event: BaseMessageEvent, allow: frozenset[str]
) -> list:
    attachments = list(await asyncio.to_thread(extract_image_video, event))
    seen: set[str] = set()

    async def add_fid(fid: str, fname: str) -> None:
        if fid in seen:
            return
        seen.add(fid)
        attachments.append(await file_id_to_binary(bot_api, event, fid, fname, allow))

    for st, sd in iter_segments(event):
        if st == "file":
            await add_fid((sd.get("file_id") or "").strip(), sd.get("file") or "")
    for m in re.finditer(
        r"\[CQ:file,file=([^,\]]+),file_id=([^,\]]+)",
        getattr(event, "raw_message", "") or "",
    ):
        await add_fid(m.group(2), m.group(1))
    return attachments
