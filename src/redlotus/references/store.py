from __future__ import annotations

import asyncio
import hashlib
import json
import mimetypes
import os
from pathlib import Path

from filelock import AsyncFileLock

from redlotus.infra.paths import references_dir
from redlotus.infra.persist_utils import (
    atomic_write_json,
    atomic_write_bytes,
    finish_file_io,
)
from redlotus.ModelGateway.input_policy import ModelInputPolicy
from redlotus.references.models import ReferenceFile, ReferencePart
from redlotus.references.readers import DocumentReader
from redlotus.runtime.context import WorkspaceContext


class ReferenceStore:
    PARSER_VERSION = 2

    def __init__(self, workspace: WorkspaceContext, root: Path | None = None):
        self.workspace = workspace
        self.root = root or references_dir()

    async def prepare_message(self, message):
        import base64
        import mimetypes
        from pydantic_ai import BinaryContent, ImageUrl, VideoUrl
        from redlotus.ModelGateway.input_policy import ModelInputPolicy

        policy = ModelInputPolicy.for_role("coordinator")
        references = list(message.references)
        for index, item in enumerate(message.attachments):
            if isinstance(item, BinaryContent):
                name = item.identifier or f"attachment-{index}"
                if not Path(name).suffix:
                    name += mimetypes.guess_extension(item.media_type) or ".bin"
                ref = await self.import_bytes(
                    item.data, name=name, source=f"attachment:{name}", policy=policy
                )
            elif isinstance(item, (ImageUrl, VideoUrl)):
                if item.url.startswith("data:"):
                    header, encoded = item.url.split(",", 1)
                    mime = header[5:].split(";")[0]
                    name = "attachment" + (mimetypes.guess_extension(mime) or ".bin")
                    ref = await self.import_bytes(
                        base64.b64decode(encoded),
                        name=name,
                        source=f"attachment:{index}",
                        policy=policy,
                    )
                else:
                    ref = await self.import_url(item.url, policy=policy)
            else:
                raise ValueError(f"Unsupported attachment type: {type(item).__name__}")
            references.append(ref)
        references = list({ref.id: ref for ref in references}.values())
        policy.check([ref.byte_size for ref in references])
        message.references, message.attachments = references, []

    async def capture_file(
        self, path: Path, *, policy: ModelInputPolicy
    ) -> ReferenceFile:
        policy.check([path.stat().st_size])
        data = await asyncio.to_thread(path.read_bytes)
        return await self.capture_bytes(
            data, name=path.name, source=str(path), policy=policy
        )

    async def import_file(
        self, path: Path, *, policy: ModelInputPolicy
    ) -> ReferenceFile:
        return await self.parse(await self.capture_file(path, policy=policy))

    async def import_url(
        self, url: str, *, policy: ModelInputPolicy, media_type: str = ""
    ) -> ReferenceFile:
        import httpx
        from urllib.parse import urlsplit, unquote
        from redlotus.infra.shared_http import get_client

        if urlsplit(url).scheme not in ("https", "http"):
            raise ValueError("Remote references require HTTP(S)")
        client = get_client(
            "reference_download",
            lambda: httpx.AsyncClient(timeout=60, follow_redirects=True),
        )
        async with client.stream("GET", url) as response:
            response.raise_for_status()
            data = bytearray()
            async for chunk in response.aiter_bytes():
                data.extend(chunk)
                policy.check([len(data)])
            mime = media_type or response.headers.get("content-type", "").split(";")[0]
        name = Path(unquote(urlsplit(url).path)).name or "attachment"
        if not Path(name).suffix:
            name += mimetypes.guess_extension(mime) or ".bin"
        return await self.import_bytes(
            bytes(data), name=name, source=url, policy=policy
        )

    async def import_bytes(
        self, data: bytes, *, name: str, source: str, policy: ModelInputPolicy
    ) -> ReferenceFile:
        reference = await self.capture_bytes(
            data, name=name, source=source, policy=policy
        )
        return await self.parse(reference)

    async def capture_bytes(
        self, data: bytes, *, name: str, source: str, policy: ModelInputPolicy
    ) -> ReferenceFile:
        policy.check([len(data)])
        digest = hashlib.sha256(data).hexdigest()
        identity = hashlib.sha256(
            f"{self.workspace.project_id}\0{os.path.normcase(source)}\0{digest}".encode()
        ).hexdigest()[:32]
        directory = self.root / "blobs" / digest[:32]
        directory.mkdir(parents=True, exist_ok=True)
        snapshot = directory / ("source" + Path(name).suffix.lower())
        async with AsyncFileLock(directory / ".build.lock", run_in_executor=False):
            if not snapshot.exists():
                await finish_file_io(
                    asyncio.to_thread(atomic_write_bytes, snapshot, data)
                )
        return ReferenceFile(
            id=identity,
            project_id=self.workspace.project_id,
            name=name,
            source=source,
            media_type=mimetypes.guess_type(name)[0] or "application/octet-stream",
            byte_size=len(data),
            sha256=digest,
            snapshot=snapshot,
        )

    async def parse(self, reference: ReferenceFile) -> ReferenceFile:
        manifest = self.root / "manifests" / f"{reference.id}.json"
        manifest.parent.mkdir(parents=True, exist_ok=True)
        async with AsyncFileLock(str(manifest) + ".lock", run_in_executor=False):
            if manifest.is_file():
                cached = ReferenceFile.model_validate_json(
                    manifest.read_text(encoding="utf-8")
                )
                if cached.parser_version == self.PARSER_VERSION:
                    return cached
            snapshot = reference.snapshot
            directory = snapshot.parent
            parts_path = directory / (
                f"parts-v{self.PARSER_VERSION}" + snapshot.suffix + ".json"
            )
            async with AsyncFileLock(directory / ".build.lock", run_in_executor=False):
                if parts_path.is_file():
                    parts = [
                        ReferencePart.model_validate(item)
                        for item in json.loads(parts_path.read_text(encoding="utf-8"))
                    ]
                else:
                    if reference.media_type.startswith(("image/", "video/", "audio/")):
                        parts = await finish_file_io(
                            asyncio.to_thread(self._media_parts, reference)
                        )
                    else:
                        parts = await DocumentReader().read(snapshot, directory)
                    await finish_file_io(
                        asyncio.to_thread(
                            atomic_write_json,
                            parts_path,
                            [part.model_dump(mode="json") for part in parts],
                        )
                    )
            prepared = reference.model_copy(
                update={"parts": parts, "parser_version": self.PARSER_VERSION}
            )
            await finish_file_io(
                asyncio.to_thread(atomic_write_json, manifest, prepared.manifest())
            )
            return prepared

    @staticmethod
    def _media_parts(reference: ReferenceFile) -> list[ReferencePart]:
        kind = reference.media_type.split("/")[0]
        path, media_type = reference.snapshot, reference.media_type
        if kind == "image":
            from PIL import Image

            with Image.open(path) as picture:
                picture.verify()
            if media_type not in ("image/png", "image/jpeg", "image/webp", "image/gif"):
                with Image.open(path) as picture:
                    path = path.parent / "image.png"
                    picture.save(path)
                media_type = "image/png"
        elif kind == "video":
            with path.open("rb") as stream:
                header = stream.read(16)
            if not (
                header[4:8] == b"ftyp" or header.startswith((b"RIFF", b"\x1aE\xdf\xa3"))
            ):
                raise ValueError("视频容器无效或未识别，不能作为原生视频提交。")
        return [
            ReferencePart(kind=kind, path=path, media_type=media_type, locator="原件")
        ]

    def load(self, reference_id: str) -> ReferenceFile:
        if len(reference_id) != 32 or any(
            c not in "0123456789abcdef" for c in reference_id
        ):
            raise ValueError("引用文件 ID 无效。")
        path = self.root / "manifests" / f"{reference_id}.json"
        return ReferenceFile.model_validate_json(path.read_text(encoding="utf-8"))

    async def read_reference(self, reference_id: str):
        """Read a previously registered reference in this project, including native media."""
        from pydantic_ai import ToolReturn

        try:
            reference = await asyncio.to_thread(self.load, reference_id)
            if reference.project_id != self.workspace.project_id:
                return "Error: Reference belongs to another project; retrieve its authorized memory record instead."
            reference = await self.parse(reference)
        except (OSError, ValueError) as exc:
            return f"Error reading reference '{reference_id}': {exc}"
        return ToolReturn(
            return_value=f"Read reference {reference.name} ({reference.id})",
            content=await asyncio.to_thread(reference.to_prompt),
        )
