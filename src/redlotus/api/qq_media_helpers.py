"""QQ 媒体解析与下载（供 QQ.py 使用，减小主文件体积）。"""

import asyncio
import os
import re
import html
import base64
import ipaddress
import mimetypes
import socket
from typing import Any

import httpx
from ncatbot.core import BaseMessageEvent, GroupMessageEvent
from pydantic_ai import BinaryContent

from redlotus.core import config as logger
from redlotus.core.gateway import ModelInputPolicy


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


def coerce_mm(url: str, header_ct: str, raw: bytes) -> str:
    ct = (header_ct or "").split(";")[0].strip().lower()
    if ct.startswith("image/") and ct != "image/octet-stream":
        return ct
    if ct.startswith("video/") and ct != "video/octet-stream":
        return ct
    g, _ = mimetypes.guess_type(url)
    return g if g and g.startswith(("image/", "video/")) else mime_magic(raw)


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
    mm = coerce_mm(url, header_ct, raw)
    return mm if mm else "application/octet-stream"


_MAX_REDIRECTS = 5


def _resolve_public_addr(host: str) -> str | None:
    """解析主机名并校验：仅当所有解析结果都是公网地址时，返回首个已校验 IP（否则 None）。

    返回已校验的 IP 供调用方直接拨号，使「校验」与「连接」共用同一次解析结果，
    杜绝 DNS rebinding（校验时返回公网 IP、连接时改返内网 IP）的 TOCTOU 绕过。
    """
    try:
        infos = socket.getaddrinfo(host, None)
    except OSError:
        return None
    chosen: str | None = None
    for info in infos:
        addr = info[4][0]
        try:
            ip = ipaddress.ip_address(addr)
        except ValueError:
            return None
        if (
            not ip.is_global
            or ip.is_private
            or ip.is_loopback
            or ip.is_link_local
            or ip.is_reserved
            or ip.is_multicast
        ):
            return None
        if chosen is None:
            chosen = addr
    return chosen


def download_to_binary(url: str, filename: str = "") -> BinaryContent | None:
    url = norm_url(url)
    try:
        with httpx.Client(timeout=30, follow_redirects=False) as client:
            for _ in range(_MAX_REDIRECTS + 1):
                parsed = httpx.URL(url)
                if parsed.scheme not in ("http", "https") or not parsed.host:
                    return None
                ip = _resolve_public_addr(parsed.host)
                if ip is None:
                    logger.warning(f"[QQ] 拒绝下载非公网媒体地址 {url[:80]}")
                    return None
                host_header = (
                    parsed.host
                    if parsed.port is None
                    else f"{parsed.host}:{parsed.port}"
                )
                resp = client.get(
                    parsed.copy_with(host=ip),
                    headers={"Host": host_header},
                    extensions={"sni_hostname": parsed.host},
                )
                location = resp.headers.get("location")
                if resp.is_redirect and location:
                    url = str(parsed.join(location))
                    continue
                resp.raise_for_status()
                hc = resp.headers.get("content-type", "").split(";")[0].strip()
                raw = resp.content
                ModelInputPolicy.for_role().check([len(raw)])
                return BinaryContent(
                    data=raw,
                    media_type=pick_ct(url, hc, raw, filename=filename),
                    identifier=filename or None,
                )
        logger.warning(f"[QQ] 重定向次数过多 {url[:80]}")
        return None
    except Exception as e:
        logger.warning(f"[QQ] 下载媒体失败 {url[:80]}: {e}")
        return None


def binary_b64(file_val: str) -> BinaryContent | None:
    if not file_val or not file_val.startswith("base64://"):
        return None
    try:
        return BinaryContent(
            data=base64.b64decode(file_val[9:]), media_type="image/png"
        )
    except Exception as e:
        logger.warning(f"[QQ] 解析 base64 图片失败: {e}")
        return None


def iter_segments(event: BaseMessageEvent):
    msg = getattr(event, "message", None)
    if not msg or not hasattr(msg, "__iter__"):
        msg = []
    for seg in msg:
        if isinstance(seg, dict):
            yield seg.get("type", ""), seg.get("data", {})
        else:
            sd = getattr(seg, "data", {})
            if not isinstance(sd, dict):
                sd = vars(sd) if hasattr(sd, "__dict__") else {}
            yield getattr(seg, "type", ""), sd


def extract_image_video(event: BaseMessageEvent) -> list[Any]:
    urls, attachments = [], []
    for seg_type, seg_data in iter_segments(event):
        if seg_type not in ("image", "video"):
            continue
        fv = seg_data.get("file") or ""
        bc = binary_b64(fv)
        if bc:
            attachments.append(bc)
            continue
        u = norm_url(seg_data.get("url") or fv)
        if u.startswith("http"):
            urls.append(u)
    if not urls:
        raw = getattr(event, "raw_message", "") or ""
        for m in re.finditer(r"\[CQ:(?:image|video),[^\]]*url=([^\],]+)", raw):
            urls.append(norm_url(m.group(1)))
    for u in urls:
        bc = download_to_binary(u)
        if bc:
            attachments.append(bc)
    return attachments


async def file_id_to_binary(
    bot_api, event: BaseMessageEvent, file_id: str, filename: str, allow: frozenset[str]
) -> BinaryContent | None:
    ext = os.path.splitext(filename or "")[1].lower()
    if not file_id or ext not in allow:
        return None
    try:
        url = await (
            bot_api.get_group_file_url(event.group_id, file_id)
            if isinstance(event, GroupMessageEvent)
            else bot_api.get_private_file_url(file_id)
        )
    except Exception as e:
        logger.warning(f"[QQ] 获取文件 URL 失败 file_id={file_id[:24]}...: {e}")
        return None
    if not url:
        logger.warning(f"[QQ] get_*_file_url 返回空，file_id={file_id[:24]}...")
        return None
    return await asyncio.to_thread(download_to_binary, url, filename)


async def extract_media(
    bot_api, event: BaseMessageEvent, allow: frozenset[str]
) -> list:
    attachments = list(await asyncio.to_thread(extract_image_video, event))
    seen: set[str] = set()

    async def add_fid(fid: str, fname: str) -> None:
        if not fid or fid in seen:
            return
        seen.add(fid)
        bc = await file_id_to_binary(bot_api, event, fid, fname, allow)
        if bc:
            attachments.append(bc)

    for st, sd in iter_segments(event):
        if st == "file":
            await add_fid((sd.get("file_id") or "").strip(), sd.get("file") or "")
    for m in re.finditer(
        r"\[CQ:file,file=([^,\]]+),file_id=([^,\]]+)",
        getattr(event, "raw_message", "") or "",
    ):
        await add_fid(m.group(2), m.group(1))
    return attachments
