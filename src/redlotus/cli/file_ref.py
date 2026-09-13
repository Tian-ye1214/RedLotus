"""Parse only the original user input, then build immutable typed references."""

from __future__ import annotations

import asyncio
import os
import re
import unicodedata
from pathlib import Path

from redlotus.workspace.workspace import current_workspace
from redlotus.runtime.context import WorkspaceContext
from redlotus.ModelGateway.input_policy import ModelInputPolicy
from redlotus.references.models import ReferenceFile
from redlotus.references.store import ReferenceStore


def _resolve_ref_path(ref: str, root: Path) -> Path:
    return (root / Path(ref).expanduser()).resolve()


def _looks_like_inline_text_suffix(suffix: str) -> bool:
    return bool(
        suffix
        and suffix[0] not in ".-_/\\"
        and (
            re.match(r"[\u4e00-\u9fff\u3040-\u30ff\uac00-\ud7af]", suffix)
            or unicodedata.category(suffix[0]).startswith("P")
        )
    )


def _resolve_existing_ref_prefix(ref: str, root: Path) -> tuple[str, Path] | None:
    for end in range(len(ref) - 1, 0, -1):
        suffix = ref[end:]
        if not _looks_like_inline_text_suffix(suffix):
            continue
        prefix = ref[:end]
        try:
            path = _resolve_ref_path(prefix, root)
        except (OSError, RuntimeError, ValueError):
            continue
        if path.exists():
            return prefix, path
    return None


def parse_file_paths(text: str, *, root: Path | None = None) -> list[Path]:
    root = root or current_workspace()
    token = re.compile(
        r"(?<![A-Za-z0-9._%+-])@(?:\{([^}]+)\}|\"([^\"]+)\"|'([^']+)'|([^\s]+))"
    )
    candidates = []
    for match in token.finditer(text):
        value = next(v for v in match.groups() if v is not None).strip()
        path = _resolve_ref_path(value, root)
        if not path.exists() and match.group(4):
            recovered = _resolve_existing_ref_prefix(value, root)
            if recovered:
                _, path = recovered
        candidates.append(path)
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
    for match in re.finditer(r'"([^"\n]+)"|\'([^\'\n]+)\'|(\S+)', token.sub(" ", text)):
        value = next(v for v in match.groups() if v is not None)
        if Path(value).suffix.lower() in media:
            path = _resolve_ref_path(value, root)
            if path.is_file():
                candidates.append(path)
    unique = {}
    for path in candidates:
        unique.setdefault(os.path.normcase(str(path)), path)
    return list(unique.values())


async def load_file_refs(
    text: str, *, role: str = "coordinator", workspace: WorkspaceContext | None = None
) -> list[ReferenceFile]:
    workspace = workspace or WorkspaceContext.from_path(current_workspace())
    paths = parse_file_paths(text, root=workspace.root)
    policy = ModelInputPolicy.for_role(role)
    if len(paths) > policy.max_files:
        raise ValueError(
            f"最多引用 {policy.max_files} 个文件，本次引用 {len(paths)} 个。"
        )
    errors, sizes = [], []
    for path in paths:
        if not path.is_file():
            errors.append(f"{path}: 文件不存在或不是普通文件")
            continue
        size = path.stat().st_size
        sizes.append(size)
        if size > policy.max_file_bytes:
            errors.append(
                f"{path}: {size:,} 字节，超过单文件限额 {policy.max_file_bytes:,} 字节"
            )
    if errors:
        raise ValueError("引用文件失败：\n" + "\n".join(errors))
    policy.check(sizes)
    store = ReferenceStore(workspace)
    captured = await asyncio.gather(
        *(store.capture_file(path, policy=policy) for path in paths)
    )
    slots = asyncio.Semaphore(4)

    async def read(reference):
        async with slots:
            try:
                return await store.parse(reference)
            except Exception as exc:
                raise ValueError(f"引用文件 {reference.name} 解析失败：{exc}") from exc

    return list(await asyncio.gather(*(read(reference) for reference in captured)))
