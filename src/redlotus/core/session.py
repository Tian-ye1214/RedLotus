"""Core session responsibilities."""

from __future__ import annotations

import asyncio
import inspect
import json
import uuid

import redlotus.runtime.resources as _runtime_resources
from redlotus.core.tasks import TaskManager
from redlotus.documents.interaction import UserMessage
from redlotus.memory.service import MemoryService
from redlotus.models.context import (
    ChatHistory,
    prepare_compression,
    repair_interrupted_tool_calls,
)
from redlotus.runtime.context import WorkspaceContext, workspace_context
from redlotus.runtime.files import finish_file_io, session_data_dir
from redlotus.storage.session import SessionFile
from redlotus.tools.base_tools import BasicToolkit
from redlotus.tools.registry import SkillsManager


class AgentSession:
    """Own durable session state, context checkpoints, and workspace transitions."""

    async def bind_session(self, session_key: str, *, storage=None, generation=None, task_title=None) -> None:
        previous = self._session_file
        if storage is None:
            storage = previous if previous and previous.session_id == session_key else await self._durable_write(lambda: SessionFile.create(
                session_data_dir(self.workspace), self.workspace.project_id,
                session_id=session_key, workspace=self.workspace,
            ))
        if storage.project_id != self.workspace.project_id or storage.session_id != session_key:
            raise ValueError("会话身份或项目不匹配")
        storage.acquire_use()
        try:
            await self._registry.ensure_agent(session_key, "coordinator")
            await self._registry.ensure_agent(session_key, "manager")
            if generation is not None and (generation != self._session.generation or self._shutdown_done):
                raise ValueError("加载已取消；目标会话未提交")
            if task_title is not None:
                _runtime_resources.setup_task_logger(task_title)
            if previous is not None and previous is not storage:
                previous.release_use()
        except BaseException:
            if storage is not previous:
                storage.release_use()
            raise
        if previous is not storage:
            self._coordinator_agent = None
            self._memory.unbind_session()
            self._memory.reset_injection_snapshot()
        self._session_key, self._session_file = session_key, storage
        self._memory.bind_session(storage)
        self._orchestrator.session_file = storage
        self._orchestrator.set_session_key(session_key)

    async def end_session_agents(self, session_key: str) -> None:
        await self._orchestrator.factory.cancel_session(session_key)
        self._session.reset(discard=True)
        await self._registry.cancel_session(session_key)
        await self._registry.remove_session(session_key)
        if self._session_key == session_key:
            self._memory.unbind_session()
            self._session_file.release_use()
            self._session_key = None
            self._session_file = None
            self._coordinator_agent = None
            self._memory.reset_injection_snapshot()
            self._orchestrator.set_session_key(None)
            self._orchestrator.session_file = None
            self._storage_paused = False

    async def reset_session(self, *, close_memory=False) -> None:
        """Release resources before discarding a conversation's recoverable state."""
        self._session.reset(discard=True)
        self.events.emit("clear_model_stream", )
        self._session.queue.discard()
        await self.cancel_current_turn()
        await self._factory.cancel_all()
        await self._toolkit.close()
        if close_memory:
            await self._memory.store.close()
        if self._session_key:
            await self.end_session_agents(self._session_key)
        self._task_manager.reset()
        self._orchestrator.plan_id = ""
        self._last_user_input = (None, "")
        self._toolkit.reset_task_directory()
        self._manager_history.reset()
        self._session_file = None
        self._memory.reset_injection_snapshot()
        self._memory.unbind_session()
        self._storage_paused = False
        self._session.take_notices()
        self._session.queue.discard()

    @property
    def registry(self):
        return self._registry

    @property
    def session_key(self) -> str | None:
        return self._session_key

    @property
    def has_current_turn(self) -> bool:
        return (
            self._current_turn is not None
            or self._session.active
            or self._session.queue.current is not None
            or self._cancel_lock.locked()
        )

    @property
    def has_current_goal_turn(self) -> bool:
        return bool(self._current_turn and self._current_turn.get("mode") == "goal")

    @property
    def is_compressing(self):
        return self._compression_future is not None and not self._compression_future.done()

    @property
    def storage_paused(self):
        return self._storage_paused

    async def retry_saved_state(self):
        """A new ordinary input permits a paused disk transaction to retry."""
        self._storage_retry.set()

    async def _durable_write(self, operation, *, cancelling=False):
        """Pause failed I/O without repeating model or tool execution."""
        with workspace_context(self.workspace):
            storage = self._session_file
            while True:
                self._storage_retry.clear()
                try:
                    if storage is not None:
                        await finish_file_io(asyncio.to_thread(storage.retry_pending))
                    result = await finish_file_io(asyncio.to_thread(operation))
                    if inspect.isawaitable(result):
                        result = await result
                    self._storage_paused = False
                    return result
                except OSError as exc:
                    self._storage_paused = True
                    self.events.emit("print_warning", f"保存失败，任务已暂停，输入已保留: {exc}。恢复存储后提交普通输入重试。")
                    if cancelling or asyncio.current_task().cancelling():
                        raise asyncio.CancelledError() from exc
                    await self._storage_retry.wait()

    async def _save_task_plan(self):
        """Serialize owner-loop snapshots before another task can consume their results."""
        async with self._task_save_lock:
            storage = self._session_file
            if storage is not None:
                snapshot = self._task_manager.snapshot()
                plan_id = self._orchestrator.plan_id
                await self._durable_write(lambda: storage.update(metadata={"tasks": snapshot, "task_plan_id": plan_id}))

    def _usage_scope(self):
        """Route every response, including threaded auxiliary calls, to its owning session."""
        from redlotus.models.providers import MODEL_USAGE_SINK
        from redlotus.runtime.context import bind_context, bind_to_loop

        storage, invocation = self._session_file, self._cli_turn_id or uuid.uuid4().hex
        async def save(role, response, *, cancelling=False):
            await self._durable_write(lambda: storage.record_usage([response], role=role, invocation=invocation), cancelling=cancelling)
        return bind_context(MODEL_USAGE_SINK, bind_to_loop(save, asyncio.get_running_loop()))

    async def cancel_compression(self):
        """Invalidate the queued control operation without cancelling ordinary tasks."""
        future = self._compression_future
        if future is not None and not future.done():
            self._session.reset()
            future.cancel()
            await self._session.queue.cancel()

    async def compress_context(self, history):
        """Serialize detached compression and persist its candidate before publishing it."""
        if self.is_compressing:
            return ["上下文压缩正在处理中。"]
        if self.has_current_turn or self._session.queue.pending:
            return ["当前任务正在运行，请先停止或等待完成。"]
        future = self._session.queue.submit(lambda: self._compress_context(history))
        self._compression_future = future
        try:
            return await asyncio.shield(future)
        except asyncio.CancelledError:
            return ["上下文压缩已取消，未提交候选不再写回。"]
        finally:
            if self._compression_future is future:
                self._compression_future = None

    async def _compress_context(self, history):
        from redlotus.core.agents import make_agent_id

        storage, generation = self._session_file, self._session.generation
        sources = {"coordinator": history, "manager": self._manager_history}
        revisions = {role: source.revision for role, source in sources.items()}
        candidates = {}

        def current():
            return (storage is self._session_file and generation == self._session.generation
                    and all(source.revision == revisions[role] for role, source in sources.items()))

        for role, source in sources.items():
            with self._usage_scope():
                candidates[role] = await prepare_compression(
                    source, role=role, force=True, retain_tail=False,
                    task_state=self.structured_task_status(),
                )
            if not current():
                return ["会话已改变，压缩候选已丢弃。"]
        if not any(candidates.values()):
            return ["当前上下文无需压缩。"]
        if storage is None:
            return ["当前没有可保存的会话，未应用压缩。"]
        coordinator = candidates["coordinator"] or history
        manager = candidates["manager"] or self._manager_history
        if manager.messages:
            await self._durable_write(lambda: storage.role_file("manager").save_context(
                manager.messages, turn_id=None,
                agent_id=make_agent_id(storage.session_id, "manager", f"{self._orchestrator.plan_id}:planning" if self._orchestrator.plan_id else "planning"),
            ))
        await self._durable_write(lambda: storage.save_context(
            coordinator.messages, turn_id=None,
        ))
        if not current():
            return ["会话已改变，压缩候选不再应用。"]
        for role, candidate in candidates.items():
            if candidate is not None:
                sources[role].set_messages(candidate.messages)
        return [f"{role}: {'已压缩并保存' if candidate else '无需压缩'}" for role, candidate in candidates.items()]

    async def shutdown(self) -> None:
        if self._shutdown_task is None:
            self._shutdown_done = True
            if self._exit_deadline is not None:
                self._exit_deadline.start()
            self._shutdown_task = asyncio.create_task(self._release_resources())
        await asyncio.shield(self._shutdown_task)

    async def _release_resources(self) -> None:
        self._session.reset(discard=True)
        self._session.queue.discard()
        self._factory.stop()
        await self.cancel_current_turn()
        if self._session_key:
            await self.end_session_agents(self._session_key)
        await self._registry.cancel_all()
        await self._factory.close()
        await self._memory.close()
        await self._toolkit.close()
        _runtime_resources.info("[lifecycle] shutdown complete")

    def set_ask_user_handler(self, handler):
        async def recorded_answer(question):
            generation = self._session.generation
            answer = (await handler(question) if inspect.iscoroutinefunction(handler)
                      else await asyncio.to_thread(handler, question))
            if isinstance(answer, str) and answer.strip() and generation == self._session.generation:
                await self.record_user_input(getattr(answer, "input_id", None) or uuid.uuid4().hex, UserMessage(answer))
            return answer

        self._toolkit.set_ask_user_handler(recorded_answer if handler else None)

    async def record_user_input(self, identity, message):
        """Persist a consumed input against its bound session, including tool questions."""
        storage = self._session_file
        if storage is not None and self._session.active:
            await self._durable_write(lambda: storage.record_input(identity, message))
            self._last_user_input = (identity, message.text)

    @property
    def review_store(self):
        return self._toolkit.review_store

    def set_task_directory(self, task_name: str):
        self._toolkit.set_task_directory(task_name)

    async def generate_task_title(self, user_text: str) -> str:
        from redlotus.models.gateway import generate_task_title

        with self._usage_scope():
            return await generate_task_title(user_text)

    def record_control_result(self, command, target, status, *, accepted):
        from pydantic_ai.messages import TextContent

        from redlotus.runtime.files import iso_utc_now

        receipt = dict(
            command=command,
            target=target,
            status=status,
            accepted=accepted,
            session_id=self._session_key,
            observed_at=iso_utc_now(),
        )
        encoded = json.dumps(receipt, ensure_ascii=False)
        if self._session_file:
            try:
                self._session_file.update(metadata={"last_control_result": receipt})
            except OSError as exc:
                self.events.emit("print_warning", f"控制回执尚未保存: {exc}")
        self._session.add_notice(
            [
                TextContent(
                    encoded, metadata={"origin": "runtime_control"}
                )
            ]
        )
        return receipt

    async def bind_loaded_snapshot(self, path, *, state, title):
        """Validate the complete candidate before replacing the current session."""
        generation = self._session.generation
        storage = SessionFile.load(path, workspace=self.workspace)
        storage.acquire_use()
        try:
            if storage.project_id != self.workspace.project_id:
                raise ValueError("会话不属于当前项目")
            metadata = storage.metadata
            task_name = metadata.get("task_name")
            if task_name is not None and not isinstance(task_name, str):
                raise ValueError("会话 task_name 必须是文本")
            tasks = TaskManager()
            tasks.restore(metadata.get("tasks", []))
            messages = storage.model_messages()
            repaired = repair_interrupted_tool_calls(messages)
            from redlotus.core.agents import make_agent_id
            manager_messages = repair_interrupted_tool_calls(storage.role_messages(
                "manager", agent_id=make_agent_id(storage.session_id, "manager", f"{metadata['task_plan_id']}:planning" if metadata.get("task_plan_id") else "planning"),
            ))
            history, manager_history = ChatHistory(), ChatHistory()
            history.set_messages(repaired)
            manager_history.set_messages(manager_messages)
            active = metadata.get("active_turn")
            if active or repaired != messages or tasks.snapshot() != metadata.get("tasks", []):
                await finish_file_io(asyncio.to_thread(lambda: storage.save_context(
                    repaired, turn_id=(active.get("turn_id") or active.get("id")) if active else None,
                    metadata={"active_turn": None, "interrupted_turn": dict(active, status="interrupted") if active else None,
                              "tasks": tasks.snapshot()},
                )))
            if generation != self._session.generation or self._shutdown_done:
                raise ValueError("加载已取消；目标会话未提交")
            previous_key = self._session_key
            await self.bind_session(storage.session_id, storage=storage, generation=generation, task_title=title)
            self._session.reset(discard=True)
            self._session.queue.discard()
            self.events.emit("clear_model_stream", )
            state.history, state.is_first_input = history, False
            self._manager_history = manager_history
            self._task_manager.tasks = tasks.tasks
            self._orchestrator.plan_id = metadata.get("task_plan_id", "")
            self._last_user_input = (None, "")
            self._toolkit.reset_task_directory()
            if task_name:
                self._toolkit.set_task_directory(task_name)
            self._current_attachments = []
            self._storage_paused = False
            async def release_previous():
                results = await asyncio.gather(
                    self._factory.cancel_all(), self._toolkit.close(), return_exceptions=True,
                )
                if previous_key and previous_key != storage.session_id:
                    try:
                        await self._registry.cancel_session(previous_key)
                        await self._registry.remove_session(previous_key)
                    except Exception as exc:
                        results.append(exc)
                return results

            cleanup = asyncio.create_task(release_previous())
            try:
                results = await asyncio.shield(cleanup)
            except asyncio.CancelledError:
                results = await cleanup
            for result in results:
                if isinstance(result, BaseException):
                    self.events.emit("print_warning", f"会话已加载，但旧资源释放未完成: {result}")
            return repaired
        finally:
            if self._session_file is not storage:
                storage.release_use()

    def structured_task_status(self) -> str:
        return self._task_manager.structured_status()

    def add_urgent(self, text: str) -> bool:
        if not self._session.accepting_urgent:
            return False
        self._session.add_notice(text)
        return True

    async def add_urgent_message(
        self, message: UserMessage, *, admission=None, references=None
    ) -> bool:
        admission = admission or self._session.admit(self.workspace, urgent=True)
        if not admission.urgent or not self._session.accepts(admission):
            return False
        store = self._toolkit._references

        async def prepare():
            try:
                if references is not None:
                    message.references = await references
                if not self._session.accepts(admission):
                    return None
                await store.prepare_message(message)
                return message if self._session.accepts(admission) else None
            except (OSError, ValueError) as exc:
                if self._session.accepts(admission):
                    self.events.emit("input_rejected", message.original_text or message.text)
                    self.events.emit("print_warning", str(exc))
                return None

        self._session.queue_urgent(admission, prepare())
        _runtime_resources.info_file_only(
            "input admitted id=%s sequence=%s turn=%s urgent=True",
            admission.id,
            admission.sequence,
            admission.turn_id,
        )
        return True

    async def _take_inner_inputs(self):
        prompts = []
        for admission, message in await self._session.take_urgent():
            await self.record_user_input(admission.id, message)
            self._session.user_inputs.append(message.original_text or message.text)
            prompts.append(message.to_prompt())
            _runtime_resources.debug(
                "input consumed id=%s sequence=%s turn=%s",
                admission.id,
                admission.sequence,
                admission.turn_id,
            )
            if self._memory.current is not None:
                event = self._memory.current
                event.reference_ids = list(
                    dict.fromkeys(
                        [*event.reference_ids, *(ref.id for ref in message.references)]
                    )
                )
                await self._durable_write(lambda: self._memory.observations.save(event))
            self._current_attachments.extend(message.attachments)
            self._current_attachments.extend(
                part for ref in message.references for part in ref.to_prompt()
            )
        return [
            *prompts,
            *self._session.take_notices(),
            *self._memory.take_context_notices(),
        ]

    async def switch_workspace(self, path) -> None:
        """Prepare the target before releasing or replacing the current conversation."""
        workspace = WorkspaceContext.from_path(path)
        if not workspace.root.is_dir():
            raise NotADirectoryError(workspace.root)
        log_dir = _runtime_resources.prepare_log_dir(workspace)
        skills = SkillsManager(workspace=workspace)
        memory = MemoryService(
            workspace=workspace,
            owner_memory_allowed=self._owner_memory_allowed,
            factory=self._factory,
            registry_factory=type(self._registry),
        )
        memory.bind_runner(
            self._registry, input_source=lambda: self._session.user_inputs
        )
        toolkit = BasicToolkit(
            skills, workspace=workspace, events=self.events,
        )
        toolkit.set_ask_user_handler(self._toolkit._ask_user_handler)
        review_store = self.review_store
        toolkit._review_store, toolkit._file_lock = review_store, review_store._lock
        await self.reset_session(close_memory=True)
        self.workspace, self._memory, self._toolkit, self._skills_manager = workspace, memory, toolkit, skills
        self._coordinator_agent = None
        from redlotus.runtime.context import set_workspace

        set_workspace(workspace.root)
        _runtime_resources.activate_log_dir(log_dir)
        review_store.clear()
        self._orchestrator._toolkit = toolkit
        self._orchestrator.memory = memory

    async def _commit_request_context(self, messages):
        """Commit an automatic checkpoint before the SDK can send or adopt it."""
        storage = self._session_file
        session_id = self._session_key
        generation = self._session.generation
        candidate = ChatHistory()
        candidate.set_messages(messages)
        await self._checkpoint(candidate, self._cli_turn_id)
        if storage is not self._session_file or session_id != self._session_key or generation != self._session.generation:
            raise asyncio.CancelledError("Session changed during checkpoint persistence.")

    async def _checkpoint(self, history, turn_id):
        """Append context and task changes to the session's sole recovery file."""
        storage, messages = self._session_file, list(history.messages)
        await self._durable_write(lambda: storage.save_context(
            messages,
            turn_id=turn_id,
        ))
