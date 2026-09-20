"""Api qq media helpers responsibilities."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ncatbot.core import BaseMessageEvent

import asyncio
import base64
import html
import ipaddress
import mimetypes
import os
import re
import socket

import httpx
from pydantic_ai import BinaryContent

from redlotus.api.base import AttachmentError
from redlotus.models.providers import ModelInputPolicy


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


def download_to_binary(url: str, filename: str = "") -> BinaryContent:
    url = norm_url(url)
    identity = filename or url or "attachment"
    try:
        with httpx.Client(timeout=30, follow_redirects=False) as client:
            for _ in range(_MAX_REDIRECTS + 1):
                parsed = httpx.URL(url)
                if parsed.scheme not in ("http", "https") or not parsed.host:
                    raise AttachmentError(identity, "invalid download URL")
                ip = _resolve_public_addr(parsed.host)
                if ip is None:
                    raise AttachmentError(identity, "download URL is not public")
                host_header = (
                    parsed.host
                    if parsed.port is None
                    else f"{parsed.host}:{parsed.port}"
                )
                with client.stream(
                    "GET",
                    parsed.copy_with(host=ip),
                    headers={"Host": host_header},
                    extensions={"sni_hostname": parsed.host},
                ) as resp:
                    location = resp.headers.get("location")
                    if resp.is_redirect and location:
                        url = str(parsed.join(location))
                        continue
                    resp.raise_for_status()
                    policy = ModelInputPolicy.for_role()
                    if length := resp.headers.get("content-length"):
                        policy.check([int(length)])
                    chunks, size = [], 0
                    for chunk in resp.iter_bytes():
                        size += len(chunk)
                        policy.check([size])
                        chunks.append(chunk)
                    raw = b"".join(chunks)
                    return BinaryContent(
                        data=raw,
                        media_type=pick_ct(url, resp.headers.get("content-type", ""), raw, filename=filename),
                        identifier=filename or None,
                    )
        raise AttachmentError(identity, "too many redirects")
    except AttachmentError:
        raise
    except Exception as e:
        raise AttachmentError(identity, str(e)) from e


def binary_b64(file_val: str) -> BinaryContent | None:
    if not file_val or not file_val.startswith("base64://"):
        return None
    try:
        raw = base64.b64decode(file_val[9:], validate=True)
        if not raw:
            raise ValueError("empty data")
        return BinaryContent(
            data=raw, media_type="image/png"
        )
    except Exception as e:
        raise AttachmentError("base64 image", str(e)) from e


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
            urls.append((seg_type, u))
        else:
            raise AttachmentError(seg_data.get("file") or seg_type, "missing media URL")
    if not urls:
        raw = getattr(event, "raw_message", "") or ""
        for m in re.finditer(r"\[CQ:(image|video),[^\]]*url=([^\],]+)", raw):
            urls.append((m.group(1), norm_url(m.group(2))))
    for _, u in urls:
        attachments.append(download_to_binary(u))
    return attachments


async def file_id_to_binary(
    bot_api, event: BaseMessageEvent, file_id: str, filename: str, allow: frozenset[str]
) -> BinaryContent:
    from ncatbot.core import GroupMessageEvent

    ext = os.path.splitext(filename or "")[1].lower()
    if not file_id:
        raise AttachmentError(filename or "file", "missing file ID")
    if ext not in allow:
        raise AttachmentError(filename or file_id, f"unsupported file type {ext or '(none)'}")
    try:
        url = await (
            bot_api.get_group_file_url(event.group_id, file_id)
            if isinstance(event, GroupMessageEvent)
            else bot_api.get_private_file_url(file_id)
        )
    except Exception as e:
        raise AttachmentError(filename or file_id, f"could not get download URL: {e}") from e
    if not url:
        raise AttachmentError(filename or file_id, "download URL was empty")
    return await asyncio.to_thread(download_to_binary, url, filename)


async def extract_media(
    bot_api, event: BaseMessageEvent, allow: frozenset[str]
) -> list:
    attachments = list(await asyncio.to_thread(extract_image_video, event))
    seen: set[str] = set()

    async def add_fid(fid: str, fname: str) -> None:
        if fid in seen:
            return
        if not fid:
            raise AttachmentError(fname or "file", "missing file ID")
        seen.add(fid)
        bc = await file_id_to_binary(bot_api, event, fid, fname, allow)
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
