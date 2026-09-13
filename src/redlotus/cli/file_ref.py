"""Parse only the original user input, then build immutable typed references."""

from __future__ import annotations

import asyncio
import os
import re
from pathlib import Path

from redlotus.cli.reference_syntax import iter_reference_spans, resolve_ref_path
from redlotus.workspace.workspace import current_workspace
from redlotus.runtime.context import WorkspaceContext
from redlotus.ModelGateway.input_policy import ModelInputPolicy
from redlotus.references.models import ReferenceFile
from redlotus.references.store import ReferenceStore


def parse_file_paths(text: str, *, root: Path | None = None) -> list[Path]:
    root = root or current_workspace()
    candidates = []
    remaining = list(text)
    for reference in iter_reference_spans(text, root=root):
        if not reference.closed:
            raise ValueError(f"引用路径未闭合：{text[reference.start :]}")
        if value := reference.value.strip():
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
