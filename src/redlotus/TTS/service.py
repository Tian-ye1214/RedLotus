"""Fixed speech assets, shared native-engine leases, and model installation state."""
from __future__ import annotations

import asyncio
import gc
import json
import re
import shutil
import tarfile
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from contextvars import ContextVar
from dataclasses import replace
from pathlib import Path
from typing import Unpack

import httpx
from filelock import FileLock, Timeout

from redlotus.runtime import resources
from . import (FileFingerprint, InstalledModel, InstalledState, ModelKind, ModelLease, ModelSpec, ModelStage,
               PreparationStatus, PreparationUpdate, ModelFactory, SpeechBusy, SpeechModel, SpeechSettings, SpeechUnavailable)


_LEASE = ContextVar("speech_engine_lease", default=None)
_RANGE = re.compile(r"bytes (\d+)-(\d+)/(\d+)")


class ModelCatalog:
    def __init__(self):
        self._models: dict[ModelKind, tuple[ModelSpec, ...]] | None = None

    def specs(self, kind: ModelKind) -> tuple[ModelSpec, ...]:
        if self._models is None:
            data = json.loads(Path(__file__).with_name("catalog.json").read_text(encoding="utf-8"))["models"]
            self._models = {key: tuple(ModelSpec.from_dict(key, value) for value in (data[key], *data[key].get("compatible", [])))
                            for key in ModelKind}
        return self._models[ModelKind(kind)]


class _Engine:
    def __init__(self, kind: ModelKind):
        self.kind = kind
        self.lock = asyncio.Lock()
        self.preparing = asyncio.Lock()
        self.admitted = 0
        self.prepared = asyncio.Event()
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix=f"speech-{kind}")
        self.native: SpeechModel | None = None
        self.version: str | None = None
        self.lease: ModelLease | None = None


class SpeechService:
    _shared: SpeechService | None = None
    _shared_lock = threading.Lock()

    def __init__(self, config: SpeechSettings | None = None):
        self.config = config or SpeechSettings.read()
        self.root = self.config.model_dir
        self.catalog = ModelCatalog()
        self.engines = {kind: _Engine(kind) for kind in ModelKind}
        self._bootstrap_task: asyncio.Task | None = None
        self._management: set[asyncio.Task] = set()
        self._closed = False
        self._shutdown_task: asyncio.Task | None = None
        self._loop = self._loop_thread = None
        self._status = {kind: PreparationStatus(target=str(self.root / kind)) for kind in ModelKind}

    @classmethod
    def shared(cls) -> SpeechService:
        with cls._shared_lock:
            if cls._shared is None or cls._shared._closed:
                cls._shared = cls()
            return cls._shared

    @classmethod
    async def close_shared(cls) -> None:
        instance = cls._shared
        if instance is not None:
            await instance.close()
        if cls._shared is instance:
            cls._shared = None

    @asynccontextmanager
    async def _management_scope(self):
        if self._closed:
            raise SpeechUnavailable("语音服务已关闭")
        task = asyncio.current_task()
        owner = task not in self._management
        self._management.add(task)
        try:
            yield
        finally:
            if owner:
                self._management.remove(task)

    def _publish(self, kind: ModelKind, **values: Unpack[PreparationUpdate]) -> None:
        if self._loop is None or self._loop_thread == threading.get_ident():
            self._status[kind] = replace(self._status[kind], **values)
        else:
            self._loop.call_soon_threadsafe(lambda: self._publish(kind, **values))

    async def _background(self, operation, *args, **kwargs):
        self._loop, self._loop_thread = asyncio.get_running_loop(), threading.get_ident()
        return await resources.thread_work(operation, *args, **kwargs)

    def _engine(self, kind: ModelKind) -> _Engine:
        return self.engines[ModelKind(kind)]

    def _dir(self, name: str) -> Path:
        try:
            return resources.managed_subdir(self.root, name)
        except ValueError as exc:
            raise SpeechUnavailable(f"模型目录 {name} 不是根内普通目录") from exc

    def _state(self) -> InstalledState:
        path = self.root / "installed.json"
        try:
            return InstalledState.from_dict(json.loads(path.read_text(encoding="utf-8"))) if path.exists() else InstalledState()
        except (OSError, ValueError, TypeError) as exc:
            raise SpeechUnavailable(f"无法读取模型安装状态 {path}: {exc}") from exc

    def _write_state(self, change):
        path = self.root / "installed.json"
        with resources.cancellable_lock(path.with_suffix(".json.lock")):
            state = self._state()
            change(state)
            resources.atomic_write_json(path, state.to_dict())
            for kind in self.engines:
                self._publish(kind, active=state.active.get(kind), previous=state.previous.get(kind))

    def _valid(self, kind: ModelKind, record: InstalledModel) -> bool:
        spec = next((item for item in self.catalog.specs(kind)
                     if item.sha256 == record.archive_sha256), None)
        if spec is None:
            return False
        try:
            if (record.path != f"{kind}/{spec.version}" or
                    not isinstance(files := record.files, dict) or not files):
                return False
            base = resources.owned_path(self.root, record.path)
            return (all((path := resources.owned_path(base, name)).is_file() and not path.is_symlink()
                        and path.stat().st_size == detail.size and resources.file_sha256(path) == detail.sha256
                        for name, detail in files.items()) and
                    spec.resources_present(files))
        except (OSError, KeyError, TypeError, ValueError, SpeechUnavailable):
            return False

    def _known_record(self, kind: ModelKind, version: str) -> InstalledModel | None:
        return record if (record := self._state().versions[kind].get(version)) is not None and self._valid(kind, record) else None

    def _usable(self, kind: ModelKind) -> tuple[str, InstalledModel] | None:
        versions = [spec.version for spec in self.catalog.specs(kind)]
        active = self._state().active.get(kind)
        return next(((version, record) for version in dict.fromkeys(([active] if active in versions else []) + versions)
                     if (record := self._known_record(kind, version))), None)

    def status(self) -> dict[ModelKind, PreparationStatus]:
        return {kind: replace(row) for kind, row in self._status.items()}

    def bootstrap(self) -> asyncio.Task | None:
        """Prepare and prewarm both engines independently, without blocking startup."""
        if self._closed:
            return None
        if self._bootstrap_task is None:
            async def warm(kind):
                try:
                    await self.prepare(kind, warm=True)
                except Exception as exc:
                    self._publish(kind, stage=ModelStage.FAILED, error=str(exc) or type(exc).__name__)
            async def bootstrap():
                # A cancelled child must not finish bootstrap before its sibling drains.
                await asyncio.gather(*(warm(kind) for kind in self.engines), return_exceptions=True)
            self._bootstrap_task = asyncio.create_task(bootstrap(), name="speech-model-prepare")
            for engine in self.engines.values():
                engine.prepared.clear()
                self._bootstrap_task.add_done_callback(lambda task, event=engine.prepared: event.set())
        return self._bootstrap_task

    async def prepare(self, kind: ModelKind | None = None, archive: Path | str | None = None, *, warm: bool = False) -> dict[ModelKind, PreparationStatus]:
        async with self._management_scope():
            if archive is not None and kind is None:
                raise ValueError("离线归档导入必须指定 asr 或 tts")
            for chosen in ((ModelKind(kind),) if kind is not None else tuple(ModelKind)):
                engine = self._engine(chosen)
                try:
                    async with engine.preparing:
                        verified = await self._background(self._prepare_one, chosen, Path(archive) if archive is not None else None)
                        engine.prepared.set()
                        if warm:
                            async with engine.lock:
                                await self._ensure_loaded(engine, verified)
                    self._publish(chosen, stage=ModelStage.READY if engine.native is not None else ModelStage.INSTALLED, error=None)
                except Exception as exc:
                    self._publish(chosen, stage=ModelStage.FAILED, error=str(exc) or type(exc).__name__)
                    if kind is not None:
                        raise
                finally:
                    engine.prepared.set()
            return self.status()

    def _prepare_one(self, kind: ModelKind, archive: Path | None, *, force: bool = False):
        ModelFactory.require_runtime()
        spec = self.catalog.specs(kind)[0]
        version = spec.version
        lookup = lambda: self._known_record(kind, version) if archive is not None or force else self._usable(kind)
        state = self._state()
        self._publish(kind, stage=ModelStage.CHECKING, active=state.active.get(kind), previous=state.previous.get(kind),
                      target=str(self.root / kind / version), error=None)
        lock_path = self._dir(".locks") / f"download-{kind}-{version}.lock"
        self._publish(kind, stage=ModelStage.WAITING)
        with resources.cancellable_lock(lock_path):
            discard_complete = archive is None
            try:
                if verified := lookup():
                    discard_complete = archive is None and (force or verified[0] == version)
                    return (version, verified) if archive is not None or force else verified
                need = spec.unpack_limit + (0 if archive is not None else spec.archive_limit - self._partial(spec)[1])
                if (free := shutil.disk_usage(self.root).free) < need:
                    raise SpeechUnavailable(f"模型目录 {self.root} 空间不足，还需 {need - free} 字节")
                source = archive if archive is not None else self._download(kind, spec)
                self._publish(kind, stage=ModelStage.VERIFYING)
                return version, self._install(kind, spec, source)
            finally:
                if discard_complete:
                    resources.discard_completed_partial(*self._download_paths(spec),
                        spec.identity, spec.archive_limit)

    def _download_paths(self, spec: ModelSpec) -> tuple[Path, Path]:
        return (part := self._dir(".downloads") / f"{spec.archive}.part"), part.with_suffix(part.suffix + ".json")

    def _partial(self, spec: ModelSpec) -> tuple[dict, int]:
        try:
            return resources.verified_partial(*self._download_paths(spec),
                                              spec.identity, spec.archive_limit)
        except ValueError as exc:
            raise SpeechUnavailable(f"模型下载缓存冲突: {exc}") from exc

    def _discard_download(self, spec: ModelSpec):
        return resources.discard_verified_partial(*self._download_paths(spec),
                                                  spec.identity, spec.archive_limit)

    def _download(self, kind: ModelKind, spec: ModelSpec) -> Path:
        part, metadata = self._download_paths(spec)
        part.parent.mkdir(parents=True, exist_ok=True)
        prior, _ = self._partial(spec)
        with httpx.Client(follow_redirects=True, timeout=httpx.Timeout(60.0, connect=20.0)) as client:
            return self._receive(kind, spec, client, prior, part, metadata)

    def _receive(self, kind, spec, client, prior, part, metadata):
        for attempt in range(2):
            resources.check_thread_cancel()
            offset = self._partial(spec)[1]
            headers = {"Range": f"bytes={offset}-"} if offset else {}
            if offset and (validator := prior.get("etag") or prior.get("last_modified")):
                headers["If-Range"] = validator
            self._publish(kind, stage=ModelStage.DOWNLOADING, bytes=offset, total=prior.get("total"))
            with client.stream("GET", spec.url, headers=headers) as response:
                if response.status_code == 416:
                    if offset and offset <= spec.archive_limit and resources.file_sha256(part) == spec.sha256:
                        return part
                    self._discard_download(spec)
                    prior = {}
                    continue
                if response.status_code not in (200, 206):
                    raise SpeechUnavailable(f"模型下载 HTTP {response.status_code}: {kind}")
                if response.status_code == 206:
                    match = _RANGE.fullmatch(response.headers.get("content-range", ""))
                    total = int(match[3]) if match else None
                    if (not match or int(match[1]) != offset or int(match[2]) < offset or total <= int(match[2]) or
                            prior.get("total") not in (None, total) or
                            (prior.get("etag") and response.headers.get("etag") != prior["etag"])):
                        self._discard_download(spec)
                        prior = {}
                        continue
                else:
                    offset = 0
                    length = response.headers.get("content-length")
                    total = int(length) if length and length.isdecimal() else None
                if total is not None and total > spec.archive_limit:
                    raise SpeechUnavailable("模型归档超过兼容清单大小上限")
                self._publish(kind, total=total, bytes=offset)
                resources.atomic_write_json(metadata, {"sha256": spec.sha256, "url": spec.url, "total": total,
                                                       "etag": response.headers.get("etag"), "last_modified": response.headers.get("last-modified")})
                with part.open("ab" if offset else "wb") as output:
                    for chunk in response.iter_bytes(65536):
                        resources.check_thread_cancel()
                        if offset + len(chunk) > spec.archive_limit:
                            output.close()
                            self._discard_download(spec)
                            raise SpeechUnavailable("模型归档超过兼容清单大小上限")
                        output.write(chunk)
                        offset += len(chunk)
                        self._publish(kind, bytes=offset)
                if total is not None and part.stat().st_size != total:
                    raise SpeechUnavailable("模型下载长度不完整，可重试续传")
                return part
        raise SpeechUnavailable("模型续传无法确认来源，请重试")

    def _install(self, kind: ModelKind, spec: ModelSpec, archive: Path):
        if not archive.is_file() or archive.stat().st_size > spec.archive_limit or resources.file_sha256(archive) != spec.sha256:
            if archive == self._download_paths(spec)[0]:
                self._discard_download(spec)
            raise SpeechUnavailable("模型归档 SHA256 不匹配")
        (stage := self._dir(".staging") / f"prepare-{uuid.uuid4().hex}").mkdir(parents=True)
        try:
            marker = {"schema": 1, "kind": kind, "version": spec.version, "archive_sha256": spec.sha256, "members": []}
            resources.atomic_write_json(stage / ".redlotus-stage.json", marker)
            with tarfile.open(archive, "r:bz2") as tar:
                try:
                    marker["members"] = resources.checked_tar_members(
                        tar, spec.root, spec.unpack_limit, reserved=(f"{spec.root}/.redlotus-installed.json",))
                except ValueError as exc:
                    raise SpeechUnavailable(f"模型{exc}") from exc
                resources.atomic_write_json(stage / ".redlotus-stage.json", marker)
                self._publish(kind, stage=ModelStage.EXTRACTING)
                for member in tar.getmembers():
                    resources.check_thread_cancel()
                    tar.extract(member, stage, filter="data")
            package = stage / spec.root
            files = {path.relative_to(package).as_posix(): FileFingerprint(path.stat().st_size, resources.file_sha256(path))
                     for path in package.rglob("*") if path.is_file()}
            version = spec.version
            record = InstalledModel(f"{kind}/{version}", spec.sha256, files)
            if not spec.resources_present(files):
                raise SpeechUnavailable("模型归档缺少必要权重或配套资源")
            resources.atomic_write_json(package / ".redlotus-installed.json", record.to_dict())
            destination = self._dir(kind) / version
            destination.parent.mkdir(parents=True, exist_ok=True)
            if destination.exists():
                marker = destination / ".redlotus-installed.json"
                if not marker.is_file() or json.loads(marker.read_text(encoding="utf-8")) != record.to_dict():
                    raise SpeechUnavailable(f"模型目标目录已存在且无法确认归属: {destination}")
                if not self._valid(kind, record):
                    state_path = self.root / "installed.json"
                    gate = self._dir(".locks") / f"gate-{kind}-{version}"
                    with resources.cancellable_lock(state_path.with_suffix(".json.lock")), resources.cancellable_lock(gate.with_suffix(gate.suffix + ".lock")):
                        if self._live_uses(kind, version) or not self._remove_recorded(kind, version, record):
                            raise SpeechUnavailable(f"模型目标目录已损坏且可能正在使用: {destination}")
                        resources.replace_retry(package, destination)
            else:
                resources.replace_retry(package, destination)
            def commit(state):
                state.versions[kind][version] = record
                old = state.versions[kind].get(active := state.active.get(kind))
                if not active or (active in {item.version for item in self.catalog.specs(kind)}
                                  and (old is None or not self._valid(kind, old))):
                    state.active[kind] = version
            self._write_state(commit)
            return record
        finally:
            shutil.rmtree(stage)
            if archive == self._download_paths(spec)[0]:
                self._discard_download(spec)

    def _load_on_worker(self, engine: _Engine, version: str, record=None):
        kind = engine.kind
        record = record or self._known_record(kind, version)
        if record is None:
            raise SpeechUnavailable(f"{kind} 模型未安装或已损坏；运行 prepare")
        gate = self._dir(".locks") / f"gate-{kind}-{version}"
        with resources.cancellable_lock(gate.with_suffix(gate.suffix + ".lock")):
            if version not in self._state().versions[kind]:
                raise SpeechUnavailable(f"{kind} 模型已清理")
            use_path = self._dir(".locks") / f"use-{kind}-{version}-{uuid.uuid4().hex}.lock"
            lease = ModelLease(resources.owned_path(self.root, record.path), use_path)
        native = None
        try:
            threads = self.config.asr_threads if kind == ModelKind.ASR else self.config.tts_threads
            native = ModelFactory.create(kind, lease.root, threads)
            self._publish(kind, stage=ModelStage.WARMING)
            native.warmup()
        except BaseException:
            if native is not None:
                native.close()
            lease.close(retain=ModelFactory.implementation(kind).retained_root() == lease.root)
            raise
        engine.native, engine.version, engine.lease = native, version, lease

    def _drop_on_worker(self, engine: _Engine):
        if engine.native is not None:
            engine.native.close()
        engine.native = None
        gc.collect()
        if engine.lease is not None:
            engine.lease.close(retain=ModelFactory.implementation(engine.kind).retained_root() == engine.lease.root)
        engine.version = engine.lease = None
        self._publish(engine.kind, loaded=None)

    def _replace_on_worker(self, engine: _Engine, candidate: str | None, record=None):
        self._drop_on_worker(engine)
        if candidate:
            self._load_on_worker(engine, candidate, record)

    async def _ensure_loaded(self, engine: _Engine, active=None):
        if engine.native is not None:
            return
        active = active or await self._background(self._usable, engine.kind)
        if not active and self._bootstrap_task is not None:
            await engine.prepared.wait()
            active = await self._background(self._usable, engine.kind)
        if not active:
            raise SpeechUnavailable(f"{engine.kind} 模型未安装；运行 prepare")
        self._publish(engine.kind, stage=ModelStage.LOADING)
        try:
            await self._background(self._load_on_worker, engine, *active)
        except BaseException as exc:
            await self._background(self._drop_on_worker, engine)
            self._publish(engine.kind, stage=ModelStage.FAILED, error=str(exc) or type(exc).__name__)
            raise
        self._publish(engine.kind, stage=ModelStage.READY, loaded=engine.version, error=None)

    @asynccontextmanager
    async def acquire(self, kind: ModelKind):
        async with self._management_scope():
            engine = self._engine(kind)
            if engine.admitted >= self.config.queue_size + 1:
                raise SpeechBusy(f"{kind} 推理队列已满")
            engine.admitted += 1
            try:
                async with engine.lock:
                    if self._closed:
                        raise SpeechUnavailable("语音服务已关闭")
                    await self._ensure_loaded(engine)
                    token = _LEASE.set((self, kind, asyncio.current_task()))
                    try:
                        yield engine.native
                    finally:
                        _LEASE.reset(token)
            finally:
                engine.admitted -= 1

    async def run(self, kind: ModelKind, operation, *args, **kwargs):
        engine = self._engine(kind)
        if _LEASE.get() != (self, kind, asyncio.current_task()):
            raise SpeechUnavailable(f"{kind} 原生推理必须在 acquire 租约内执行")
        loop = asyncio.get_running_loop()
        async def wait_native():
            return await loop.run_in_executor(engine.executor, lambda: operation(*args, **kwargs))
        return await resources.finish_io(wait_native())

    async def _switch(self, kind: ModelKind, candidate: str):
        engine = self._engine(kind)
        async with engine.lock:
            current = (await self._background(self._state)).active.get(kind)
            if candidate == current and engine.version == candidate and engine.native is not None:
                return
            if not (record := await self._background(self._known_record, kind, candidate)):
                raise SpeechUnavailable(f"{kind} 候选模型缺失或损坏")
            prior = (prior_loaded := engine.version) or (await self._background(self._usable, kind) or (None,))[0]
            self._publish(kind, stage=ModelStage.LOADING)
            try:
                await self._background(self._replace_on_worker, engine, candidate, record)
                def commit(latest):
                    latest.active[kind] = candidate
                    if prior and prior != candidate:
                        latest.previous[kind] = prior
                await self._background(self._write_state, commit)
                self._publish(kind, stage=ModelStage.READY, loaded=engine.version, error=None)
            except BaseException as exc:
                if engine.version != prior_loaded and (await self._background(self._state)).active.get(kind) != candidate:
                    await self._background(self._replace_on_worker, engine, prior_loaded)
                self._publish(kind, stage=ModelStage.READY if engine.native is not None else ModelStage.FAILED, loaded=engine.version,
                              error=str(exc) or type(exc).__name__)
                raise

    def _update_pin(self, kind):
        return resources.cancellable_lock(self._dir(".locks") / f"update-{kind}.lock", wait=False)

    async def update(self, kind: ModelKind | None = None) -> dict[ModelKind, PreparationStatus]:
        async with self._management_scope():
            for chosen in ((ModelKind(kind),) if kind is not None else tuple(ModelKind)):
                try:
                    async with resources.threaded_context(self._update_pin, chosen):
                        async with self._engine(chosen).preparing:
                            await self._background(self._prepare_one, chosen, None, force=True)
                        await self._switch(chosen, (await self._background(self.catalog.specs, chosen))[0].version)
                except Timeout as exc:
                    raise SpeechBusy(f"{chosen} 模型更新正在进行") from exc
            return self.status()

    async def rollback(self, kind: ModelKind) -> dict[ModelKind, PreparationStatus]:
        async with self._management_scope():
            self._engine(kind)
            previous = (await self._background(self._state)).previous.get(kind)
            if not previous:
                raise SpeechUnavailable(f"{kind} 没有可回滚版本")
            await self._switch(kind, previous)
            return self.status()

    def _live_uses(self, kind: ModelKind, version: str) -> bool:
        return resources.locks_in_use(self._dir(".locks").glob(f"use-{kind}-{version}-*.lock"))

    def _remove_recorded(self, kind: ModelKind, version: str, record: InstalledModel) -> bool:
        try:
            return (record.path == f"{kind}/{version}" and
                    resources.remove_recorded_tree(resources.owned_path(self.root, record.path),
                                                   ".redlotus-installed.json", record.to_dict(), record.files, complete=True))
        except (ValueError, TypeError, KeyError):
            return False

    def _clean_sync(self):
        path = self.root / "installed.json"
        if path.exists():
            with resources.cancellable_lock(path.with_suffix(".json.lock")):
                state = self._state()
                for kind in ModelKind:
                    try:
                        guard = FileLock(str(self._dir(".locks") / f"update-{kind}.lock")).acquire(timeout=0)
                    except Timeout:
                        continue
                    with guard:
                        protected = {state.active.get(kind), state.previous.get(kind)}
                        for version, record in list(state.versions[kind].items()):
                            if version in protected:
                                continue
                            gate = self._dir(".locks") / f"gate-{kind}-{version}"
                            with resources.cancellable_lock(gate.with_suffix(gate.suffix + ".lock")):
                                if not self._live_uses(kind, version) and self._remove_recorded(kind, version, record):
                                    del state.versions[kind][version]
                resources.atomic_write_json(path, state.to_dict())
        for kind, spec in ((kind, spec) for kind in ModelKind for spec in self.catalog.specs(kind)):
            lock_path = self._dir(".locks") / f"download-{kind}-{spec.version}.lock"
            lock_path.parent.mkdir(parents=True, exist_ok=True)
            self._dir(".locks")
            try:
                with FileLock(str(lock_path)).acquire(timeout=0):
                    self._discard_download(spec)
            except Timeout:
                pass
        self._clean_staging()

    def _clean_staging(self):
        staging = self._dir(".staging")
        for stage in staging.glob("prepare-*"):
            if not resources.ordinary_owned_directory(staging, stage):
                continue
            try:
                marker = json.loads((stage / ".redlotus-stage.json").read_text(encoding="utf-8"))
                kind = marker["kind"]
                spec = next(item for item in self.catalog.specs(kind)
                            if item.version == marker["version"] and item.sha256 == marker["archive_sha256"])
                if marker["schema"] != 1 or not isinstance(names := marker["members"], list) or not all(
                        isinstance(name, str) and resources.safe_tar_name(name, spec.root) for name in names):
                    continue
                lock = self._dir(".locks") / f"download-{kind}-{spec.version}.lock"
                with FileLock(str(lock)).acquire(timeout=0):
                    resources.remove_recorded_tree(stage, ".redlotus-stage.json", marker, names)
            except (OSError, ValueError, TypeError, KeyError, StopIteration, Timeout):
                continue

    async def clean(self) -> dict[ModelKind, PreparationStatus]:
        async with self._management_scope():
            await self._background(self._clean_sync)
            return self.status()

    async def close(self):
        if self._shutdown_task is None:
            self._closed = True
            self._shutdown_task = asyncio.create_task(self._shutdown())
        await resources.finish_io(asyncio.wait({self._shutdown_task}))
        await self._shutdown_task

    async def _shutdown(self):
        pending = {task for task in (*self._management, self._bootstrap_task) if task is not None and not task.done()}
        pending.discard(asyncio.current_task())
        for task in pending:
            task.cancel()
        if pending:
            await resources.finish_io(asyncio.wait(pending))
        for engine in self.engines.values():
            async with engine.lock:
                await self._background(self._drop_on_worker, engine)
                await asyncio.to_thread(engine.executor.shutdown, True)
