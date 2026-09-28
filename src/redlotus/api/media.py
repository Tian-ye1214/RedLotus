"""Channel media preparation: resolve local references and download ordered SDK attachments."""

from __future__ import annotations

import asyncio
import base64
import html
import ipaddress
import mimetypes
import os
import unicodedata
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
import re
import socket
from contextlib import ExitStack, aclosing
from functools import partial
from typing import TYPE_CHECKING

import httpx
from pydantic_ai import BinaryContent

from redlotus.runtime.network import ModelInputPolicy
from redlotus.runtime.resources import WorkspaceContext, current_workspace, finish_io
from redlotus.sessions.context import UserMessage

if TYPE_CHECKING:
    from ncatbot.core import BaseMessageEvent
    from redlotus.tools.references import ReferenceFile


def norm_url(url: str) -> str:
    return html.unescape((url or "").strip())


def mime_magic(raw: bytes) -> str:
    if raw.startswith((b"\x02#!SILK_V3", b"#!SILK_V3")):
        return "audio/silk"
    if len(raw) >= 12 and raw[:4] == b"RIFF" and raw[8:12] == b"WAVE":
        return "audio/wav"
    for signature, mime in ((b"\xff\xd8\xff", "image/jpeg"), (b"\x89PNG", "image/png"),
                            (b"GIF87a", "image/gif"), (b"GIF89a", "image/gif"), (b"BM", "image/bmp")):
        if raw.startswith(signature):
            return mime
    if len(raw) >= 12 and raw[:4] == b"RIFF" and raw[8:12] == b"WEBP":
        return "image/webp"
    return "video/mp4" if len(raw) >= 12 and raw[4:8] == b"ftyp" else ""


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


class _PublicDownloadTransport(httpx.BaseTransport):
    """Pin each native redirect request to validated public addresses."""

    def __init__(self):
        self._clients = ExitStack()

    def close(self):
        self._clients.close()

    def handle_request(self, request):
        parsed = request.url
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
        client = self._clients.enter_context(httpx.Client(verify=context))
        for ip in addresses:
            try:
                return client.send(httpx.Request(
                    request.method, parsed.copy_with(host=ip), headers=request.headers,
                    extensions=request.extensions | {"sni_hostname": parsed.host},
                ), stream=True)
            except (httpx.ConnectError, httpx.ConnectTimeout, httpx.ProxyError):
                if ip == addresses[-1]:
                    raise


def download_to_binary(url: str, filename: str = "") -> BinaryContent:
    url = norm_url(url)
    try:
        policy = ModelInputPolicy.for_role()
        with httpx.Client(transport=_PublicDownloadTransport(), follow_redirects=True) as client:
            with client.stream("GET", url) as resp:
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
                    media_type=pick_ct(str(resp.url), resp.headers.get("content-type", ""), raw, filename=filename),
                    identifier=filename or None,
                )
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
    segments = getattr(event, "message", None)
    if segments:
        for seg in segments:
            yield (seg.get("type", ""), seg.get("data", {})) if isinstance(seg, dict) else (seg.msg_seg_type, vars(seg))
    else:
        for match in re.finditer(r"\[CQ:(\w+)(?:,([^\]]*))?\]", getattr(event, "raw_message", "") or ""):
            yield match[1], dict(field.split("=", 1) for field in (match[2] or "").split(",") if "=" in field)


async def file_id_to_binary(bot_api, event: BaseMessageEvent, file_id: str, filename: str) -> BinaryContent:
    if not file_id:
        raise ValueError(f"附件 {filename} 缺少文件 ID；请重新发送完整消息。")
    try:
        url = await (bot_api.get_group_file_url(event.group_id, file_id) if event.is_group_msg()
                     else bot_api.get_private_file_url(file_id))
        if not url:
            raise ValueError("下载地址为空")
        return await asyncio.to_thread(download_to_binary, url, filename)
    except Exception as exc:
        raise ValueError(f"附件 {filename}（{file_id}）无法获取下载内容：{exc}") from exc


async def record_to_binary(bot_api, data: dict, filename: str) -> BinaryContent:
    """Prefer actual SILK/WAV bytes; ask NapCat for WAV only when needed."""
    file, file_id, url = (str(data.get(key) or "").strip() for key in ("file", "file_id", "url"))
    if file.startswith("base64://"):
        direct = binary_b64(file)
        mime = mime_magic(direct.data)
        if mime not in {"audio/silk", "audio/wav"}:
            raise ValueError("语音内容不是可解析的 SILK 或 WAV")
        return BinaryContent(direct.data, media_type=mime, identifier=filename)
    if url or file.startswith(("http://", "https://")):
        direct = await asyncio.to_thread(download_to_binary, url or file, filename)
        if (mime := mime_magic(direct.data)) in {"audio/silk", "audio/wav"}:
            return BinaryContent(direct.data, media_type=mime, identifier=filename)
    if not (file or file_id):
        raise ValueError("语音段缺少文件名或文件 ID")
    if not callable(getattr(bot_api, "get_record", None)):
        raise ValueError("当前 NapCat 接口不支持 get_record WAV 转换")
    converted = await bot_api.get_record(**({"file_id": file_id} if file_id else {"file": file}), out_format="wav")
    result = converted if isinstance(converted, dict) else vars(converted)
    converted_file = str(result.get("file") or "")
    converted_url = str(result.get("url") or "")
    encoded = str(result.get("base64") or "")
    if converted_url or converted_file.startswith(("http://", "https://")):
        downloaded = await asyncio.to_thread(download_to_binary, converted_url or converted_file, filename)
        raw = downloaded.data
    elif converted_file.startswith("base64://") or encoded:
        raw = base64.b64decode((converted_file[9:] if converted_file.startswith("base64://") else encoded), validate=True)
    elif converted_file and Path(converted_file).is_file():
        raw = await asyncio.to_thread(Path(converted_file).read_bytes)
    else:
        raise ValueError("NapCat 转换结果没有可读取的音频文件")
    mime = mime_magic(raw)
    if mime not in {"audio/silk", "audio/wav"}:
        raise ValueError("NapCat 转换结果不是可解析的 SILK 或 WAV")
    return BinaryContent(raw, media_type=mime, identifier=filename)


async def extract_media(bot_api, event: BaseMessageEvent) -> list:
    """Preserve SDK/raw segment order; any invalid item rejects the whole request."""
    attachments = []
    for index, (kind, data) in enumerate(iter_segments(event)):
        if kind not in {"file", "image", "video", "record"}:
            continue
        file = data.get("file") or ""
        if kind == "record":
            filename = data.get("name") or (Path(file).name if file and not file.startswith(("base64://", "http://", "https://")) else "") or f"record[{index + 1}]"
        else:
            filename = data.get("name") or file or f"{kind}[{index + 1}]"
        try:
            if kind == "file":
                item = await file_id_to_binary(bot_api, event, (data.get("file_id") or "").strip(), filename)
            elif kind == "record":
                item = await record_to_binary(bot_api, data, filename)
            elif item := binary_b64(data.get("file") or ""):
                item = BinaryContent(item.data, media_type=item.media_type, identifier=data.get("name") or f"{kind}[{index + 1}]")
            else:
                item = await asyncio.to_thread(download_to_binary, data.get("url") or data.get("file") or "", filename)
            attachments.append(item)
        except Exception as exc:
            raise ValueError(f"附件 {filename} 准备失败：{exc}；请重新发送完整消息。") from exc
    return attachments


async def transcribe_voice_message(system, message):
    """Replace admitted channel audio with one local transcript in memory."""
    from redlotus.TTS import NoSpeechDetected
    from redlotus.TTS.asr import StreamingRecognizer
    from redlotus.TTS.audio import AudioIO
    policy = ModelInputPolicy.for_role("coordinator")
    policy.check([len(item.data) for item in message.attachments if isinstance(item, BinaryContent)])
    remaining, texts = [], []
    for item in message.attachments:
        if not isinstance(item, BinaryContent) or not (item.media_type.startswith("audio/") or b"#!SILK_V3" in item.data[:12]):
            remaining.append(item)
            continue
        mime = "audio/silk" if b"#!SILK_V3" in item.data[:12] else item.media_type
        text = ""
        async with aclosing(AudioIO.parse_input(item.data, format=mime)) as pcm:
            async with aclosing(StreamingRecognizer().recognize(pcm)) as results:
                async for result in results:
                    if result.is_final:
                        text = result.text.strip()
        if not text:
            raise NoSpeechDetected("未识别到语音，请重试。")
        texts.append(text)
    message.text = "\n".join(part for part in [message.text, *texts] if part)
    message.original_text = message.text
    message.speech_body = None
    message.attachments, message.voice = remaining, False


# A period can end the preceding sentence; normal email local parts cannot end in one.
_AT_START = re.compile(r"(?<![A-Za-z0-9_%+-])@")
_WORD = re.compile(r"\S*")
_CLOSERS = {'"': '"', "'": "'", "{": "}"}


@dataclass(frozen=True)
class ReferenceSpan:
    start: int
    end: int
    value: str
    opener: str = ""
    closed: bool = True
    alternatives: tuple[str, ...] = ()


def resolve_ref_path(value: str, root: Path) -> Path:
    return (root / Path(value).expanduser()).resolve()


def _existing_path(value: str, root: Path, *, file_only=False) -> bool:
    try:
        path = resolve_ref_path(value, root)
        return bool(value) and (path.is_file() if file_only else path.exists())
    except (OSError, RuntimeError, ValueError):
        return False


def _punctuation(text: str) -> bool:
    return bool(text) and all(
        unicodedata.category(char).startswith("P") for char in text
    )


def _inline_suffix(suffix: str) -> bool:
    if suffix[0] == ".":
        return _punctuation(suffix)  # A sentence ending, never a missing .backup file.
    return suffix[0] not in "-_/\\" and bool(
        re.match(r"[\u4e00-\u9fff\u3040-\u30ff\uac00-\ud7af]", suffix)
        or unicodedata.category(suffix[0]).startswith("P")
    )


def _existing_prefix(value: str, root: Path) -> str | None:
    if _existing_path(value, root):
        return value
    return next((value[:length] for length in range(len(value) - 1, 0, -1)
                 if _inline_suffix(value[length:]) and _existing_path(value[:length], root, file_only=True)), None)


def _spaced_prefix(text: str, start: int, word_end: int, root: Path) -> str | None:
    """Extend an unquoted path across spaces only when a real file matches."""
    tail = text[word_end:].split("\n", 1)[0].split("\r", 1)[0]
    for match in re.finditer(r"\s+\S+", tail):
        end = word_end + match.end()
        candidate = _existing_prefix(text[start:end], root)
        if candidate is not None and len(candidate.rstrip()) > word_end - start:
            return candidate.rstrip()
        if "@" in match.group():
            break
    return None


def iter_reference_spans(text: str, *, root: Path) -> Iterator[ReferenceSpan]:
    cursor, adjacent = 0, False
    while cursor < len(text):
        match = _AT_START.search(text, cursor)
        if adjacent and text[cursor] == "@":
            start = cursor
        elif match is not None:
            start = match.start()
        else:
            return
        value_start = start + 1
        opener = text[value_start : value_start + 1]
        if opener in _CLOSERS:
            close = text.find(_CLOSERS[opener], value_start + 1)
            end = len(text) if close == -1 else close + 1
            yield ReferenceSpan(
                start,
                end,
                text[value_start + 1 : end if close == -1 else close],
                opener,
                close != -1,
            )
            cursor, adjacent = end, True
            continue

        word_end = _WORD.match(text, value_start).end()
        value = text[value_start:word_end]
        prefix = _existing_prefix(value, root)
        spaced = _spaced_prefix(text, value_start, word_end, root)
        alternatives = ()
        if spaced and spaced != prefix:
            if prefix and _existing_path(prefix, root, file_only=True):
                alternatives = (prefix, spaced)
            prefix = spaced
        if prefix is not None:
            # A filename can contain @; prefer the longest existing path before
            # interpreting any remaining markers as separate references.
            consumed = value_start + len(prefix)
            yield ReferenceSpan(start, consumed, prefix, alternatives=alternatives)
            cursor, adjacent = consumed, True
            continue

        separator = re.search(r"[@，。、；！？,;]", value)
        end = word_end if separator is None else value_start + separator.start()
        yield ReferenceSpan(start, end, text[value_start:end])
        cursor, adjacent = end, True


def quote_reference_path(value: str, *, opener: str = "", directory=False) -> str:
    ambiguous = any(char.isspace() or char in "@{}\"',;，；" for char in value)
    if not opener and not ambiguous:
        return value
    for quote in dict.fromkeys([opener, '"', "'", "{"]):
        if quote and _CLOSERS[quote] not in value:
            return quote + value + ("" if directory else _CLOSERS[quote])
    raise ValueError("文件名包含无法用引号或花括号包裹的分隔符。")


def parse_file_paths(text: str, *, root: Path | None = None) -> list[Path]:
    root = root or current_workspace()
    candidates = []
    remaining = list(text)
    for reference in iter_reference_spans(text, root=root):
        if reference.alternatives:
            raise ValueError(
                "引用路径存在歧义，请用引号或花括号指定："
                + "、".join(reference.alternatives)
            )
        if not reference.closed:
            raise ValueError(f"引用路径未闭合：{text[reference.start :]}")
        if value := reference.value.strip():
            if value.lower().startswith(("http://", "https://")):
                raise ValueError(f"引用仅支持本地文件：{value}")
            candidates.append((reference.start, resolve_ref_path(value, root)))
        remaining[reference.start : reference.end] = " " * (
            reference.end - reference.start
        )
    media = {
        ".png",
        ".jpg",
        ".jpeg",
        ".webp",
        ".gif",
        ".bmp",
        ".mp4",
        ".mov",
        ".mkv",
        ".avi",
        ".webm",
    }
    for match in re.finditer(r'"([^"\n]+)"|\'([^\'\n]+)\'|(\S+)', "".join(remaining)):
        value = next(v for v in match.groups() if v is not None)
        if Path(value).suffix.lower() in media:
            path = resolve_ref_path(value, root)
            if path.is_file():
                candidates.append((match.start(), path))
    unique = {}
    for _, path in sorted(candidates, key=lambda item: item[0]):
        unique.setdefault(os.path.normcase(str(path)), path)
    return list(unique.values())


async def load_file_refs(
    text: str, *, role: str = "coordinator", workspace: WorkspaceContext | None = None, captured=None
) -> list[ReferenceFile]:
    from redlotus.tools.references import ReferenceStore

    workspace = workspace or WorkspaceContext.from_path(current_workspace())
    store = ReferenceStore(workspace)
    snapshots = [store.load(key) for key in captured['reference_ids']] if captured is not None and 'reference_ids' in captured else None
    if snapshots is None:
        paths = parse_file_paths(text, root=workspace.root)
        policy = ModelInputPolicy.for_role(role) if paths else None
        if paths and len(paths) > policy.max_files:
            raise ValueError(f"最多引用 {policy.max_files} 个文件，本次引用 {len(paths)} 个。")
        sizes = [(path, path.stat().st_size if path.is_file() else None) for path in paths]
        errors = [f"{path}: " + ("文件不存在或不是普通文件" if size is None else
                  f"{size:,} 字节，超过单文件限额 {policy.max_file_bytes:,} 字节")
                  for path, size in sizes if size is None or size > policy.max_file_bytes]
        if errors:
            raise ValueError("引用文件失败：\n" + "\n".join(errors))
        async def capture():
            snapshots = await asyncio.gather(*(store.capture_file(path, policy=policy) for path in paths))
            if captured is not None:
                captured['reference_ids'] = [ref.id for ref in snapshots]
            return snapshots
        snapshots = await finish_io(capture())
    message = UserMessage(text, references=snapshots)
    if snapshots:
        await store.prepare_message(message, role=role)
    return message.references
