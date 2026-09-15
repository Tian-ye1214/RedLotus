from pathlib import Path
import asyncio
import inspect
import re
import subprocess
import threading
import mimetypes
import functools
from ddgs import DDGS
from redlotus.infra import logger
import shlex
import platform as _platform
from pydantic_ai import BinaryContent, ToolReturn
from redlotus.tools.ImageGeneration import generate_image_from_flux
from redlotus.config.app_config import get_agent_run_policy
from redlotus.skills.SkillsManager import SkillsManager
from redlotus.infra.path_sandbox import resolve_readable_path
from redlotus.runtime.context import WorkspaceContext
from redlotus.workspace.workspace import current_workspace
from redlotus.infra.paths import runtime_dir, user_skills_dir
from redlotus.infra.subprocess_runner import (
    describe_execution_environment,
    run_subprocess,
)
from redlotus.tools.browser_session import PlaywrightBrowserSession
from redlotus.cli.render import show_file_diff
from redlotus.cli.pending_review import PendingReviewStore
from redlotus.references.store import ReferenceStore


class BasicToolkit:
    def __init__(
        self,
        skills_manager: SkillsManager,
        *,
        extra_worker_tools: list | None = None,
        workspace: WorkspaceContext | None = None,
    ):
        self.workspace = workspace or WorkspaceContext.from_path(current_workspace())
        if skills_manager is not None:
            skills_manager.workspace = self.workspace
        self._clawhub_cwd = runtime_dir()
        self._skills_overlay = user_skills_dir()
        self._WORK_DATABASE_ROOT = self.workspace.root / "WorkDatabase"
        self._artifact_dir = self._WORK_DATABASE_ROOT
        self._base_dir: Path = self.workspace.root
        self._file_lock = threading.Lock()
        self._command_lock = asyncio.Lock()
        self._review_store = PendingReviewStore(self._file_lock)
        self._references = ReferenceStore(self.workspace)
        self._extra_worker_tools: list = list(extra_worker_tools or [])
        self._ask_user_handler = None
        self._skills_manager = skills_manager
        self._browser_session = PlaywrightBrowserSession(self.workspace)
        self._dangerous_patterns = [
            "rm -rf /",
            "rm -rf /*",
            "mkfs.",
            "dd if=",
            ":(){:|:&};:",
            "> /dev/sda",
            "chmod -R 777 /",
            "| sh",
            "| bash",
        ]
        self._dangerous_start_patterns = [
            "eval ",
            "exec ",
        ]
        self._confirm_patterns = [
            (re.compile(r"\brm\s+-\w*r", re.I), "rm 递归删除"),
            (re.compile(r"\brd\s+/s", re.I), "rd /s 递归删除目录"),
            (re.compile(r"\brmdir\s+/s", re.I), "rmdir /s 递归删除目录"),
            (re.compile(r"\bdel\s+/s", re.I), "del /s 递归删除文件"),
            (
                re.compile(r"\bRemove-Item\b.*-Recurse", re.I),
                "Remove-Item -Recurse 递归删除",
            ),
        ]

    @property
    def skills_manager(self) -> SkillsManager:
        return self._skills_manager

    @property
    def review_store(self) -> PendingReviewStore:
        return self._review_store

    async def close(self) -> None:
        """进程退出时关闭 Playwright 等资源。"""
        await self._browser_session.close()

    def clone_for_worker(self, owner_loop: asyncio.AbstractEventLoop) -> "BasicToolkit":
        """Create loop-local tool resources; bridge shared services to their owner loop."""

        def bridge(fn):
            @functools.wraps(fn)
            async def call(*args, **kwargs):
                future = asyncio.run_coroutine_threadsafe(
                    fn(*args, **kwargs), owner_loop
                )
                return await asyncio.wrap_future(future)

            return call

        child = BasicToolkit(
            SkillsManager(workspace=self.workspace),
            workspace=self.workspace,
            extra_worker_tools=[bridge(t) for t in self._extra_worker_tools],
        )
        child._file_lock = self._file_lock
        child._review_store = self._review_store
        child._artifact_dir = self._artifact_dir
        child.set_ask_user_handler(bridge(self.ask_user))
        return child

    def set_task_directory(self, task_name: str) -> Path:
        """
        Set a dedicated work directory for the current task.

        Parameters:
            task_name: Task name; used to create a subdirectory under WorkDatabase.

        Returns:
            Path to the task work directory.
        """
        safe_name = "".join(
            c if c.isalnum() or c in ("_", "-", " ") else "_" for c in task_name
        )
        safe_name = safe_name.strip()[:50]

        if not safe_name:
            safe_name = "default_task"

        task_dir = self._WORK_DATABASE_ROOT / safe_name
        self._artifact_dir = task_dir
        logger.info(f"📁 任务工作目录已设置: {task_dir}")

        return task_dir

    def reset_task_directory(self):
        self._base_dir = self.workspace.root
        self._artifact_dir = self._WORK_DATABASE_ROOT
        logger.info(f"📁 工作目录已重置为: {self._base_dir}")

    def _resolve_path_candidate(self, name: str) -> Path:
        return resolve_readable_path(name, work_base=self._base_dir)

    def _safe_path(self, name: str) -> Path:
        path = self._resolve_path_candidate(name)
        root = self.workspace.root
        if not path.is_relative_to(root):
            raise ValueError(f"Path not under current project: {path}")
        return path

    def _readable_path(self, name: str) -> Path:
        return self._resolve_path_candidate(name)

    def _is_command_safe(self, command: str) -> tuple[bool, str]:
        """Check if command contains dangerous patterns"""
        command_lower = command.lower().strip()
        for pattern in self._dangerous_patterns:
            if pattern.lower() in command_lower:
                return False, f"Dangerous command pattern detected: '{pattern}'"
        for pattern in self._dangerous_start_patterns:
            if command_lower.startswith(pattern.lower()):
                return False, f"Dangerous command pattern detected: '{pattern}'"
        if "npx" in command_lower and "clawhub" in command_lower:
            if re.search(r"\bclawhub\s+install(?:\s|$)", command_lower):
                return False, (
                    "Blocked bare `npx clawhub install`. Use: "
                    f"`npx clawhub --dir skills install <slug>`. Skills dir: {self._skills_overlay}"
                )
            if re.search(r'--dir(?:=|\s+)["\']?[a-zA-Z]:', command):
                return False, (
                    "Blocked `--dir` with a drive letter; use `--dir skills` (relative to the skills work dir). "
                    f"Skills dir: {self._skills_overlay}"
                )
        return True, ""

    def _command_needs_confirm(self, command: str) -> str | None:
        """Return a short reason if the command performs a recursive delete, else None."""
        for pattern, reason in self._confirm_patterns:
            if pattern.search(command):
                return reason
        return None

    def set_ask_user_handler(self, handler):
        """
        Replace the underlying implementation of ask_user (for non-terminal use, e.g. QQ Bot, Web API).
        handler may be sync or async: (question: str) -> str | None, or async (question: str) -> str | None.
        Pass None to restore the default terminal input.
        """
        self._ask_user_handler = handler

    async def ask_user(self, question: str) -> str:
        """
        Ask the user a question and return their reply.
        Parameters:
            question: The question to ask
        Returns:
            The user's answer
        """
        if self._ask_user_handler is not None:
            if inspect.iscoroutinefunction(self._ask_user_handler):
                result = await self._ask_user_handler(question)
            else:
                result = await asyncio.to_thread(self._ask_user_handler, question)
            return result if result is not None else "(User did not reply)"

        logger.info("=" * 50)
        logger.info("🤔 Agent 需要您的帮助")
        logger.info("=" * 50)
        logger.info(f"问题: {question}")

        user_response = (await asyncio.to_thread(input, "📝 您的回复: ")).strip()
        logger.info(f"用户回答: {user_response}")

        return user_response

    async def extract_text(self, name: str) -> ToolReturn | str:
        """Read a project document. For registered reference snapshots use read_reference(id)."""
        from redlotus.ModelGateway.input_policy import ModelInputPolicy

        try:
            reference = await self._references.import_file(
                self._readable_path(name), policy=ModelInputPolicy.for_role()
            )
        except (OSError, ValueError) as exc:
            return f"Error reading '{name}': {exc}. For an already registered reference, use read_reference(reference_id)."
        return ToolReturn(
            return_value={"reference_id": reference.id, "source": reference.source},
            content=reference.to_prompt(),
        )

    def read_file(self, name: str) -> str:
        """
        Read file contents.
        Parameters:
            name: File name/path
        """
        try:
            return (
                self._readable_path(name).read_text(encoding="utf-8", errors="replace")
                or "File is empty"
            )
        except (OSError, ValueError) as exc:
            return f"Error reading '{name}': {exc}"

    def list_files(self, directory: str = "") -> str:
        """
        List all files and folders in a directory.
        Parameters:
            directory: Optional, subdirectory path, defaults to root directory
        """
        try:
            target_dir = self._readable_path(directory) if directory else self._base_dir
            if not target_dir.exists():
                return f"Error: Directory '{directory}' does not exist"

            items = []
            base_r = self._base_dir.resolve()
            for item in sorted(target_dir.iterdir()):
                try:
                    rel_path = str(item.relative_to(base_r))
                except ValueError:
                    rel_path = str(item)
                if item.is_dir():
                    items.append(f"{rel_path}/")
                else:
                    size = item.stat().st_size
                    items.append(f"{rel_path} ({size} bytes)")

            return "\n".join(items) if items else "Directory is empty"
        except ValueError as e:
            return str(e)
        except Exception as e:
            return f"Error listing files: {e}"

    def _update_file(self, name, update):
        try:
            path = self._safe_path(name)
            old, content = self._review_store.write(path, name, update)
            added, deleted, modified = show_file_diff(old, content, path=name)
            return f"Saved '{name}' ({len(content)} characters; +{added} -{deleted} ~{modified})"
        except (OSError, ValueError) as exc:
            return f"Error updating '{name}': {exc}"

    def write_file(self, name: str, content: str) -> str:
        """
        Create or overwrite a file with SHORT content only.

        Parameters:
            name: Path relative to the current project; use WorkDatabase/ for generated artifacts.
            content: Content to write
        """
        return self._update_file(name, lambda previous: content)

    def edit_file(self, name: str, old_string: str, new_string: str) -> str:
        """
        Edit an existing file by replacing an EXACT, UNIQUE snippet (string replace).
        Prefer this over write_file when modifying an existing file: it makes a precise,
        local change and shows a colored diff instead of rewriting the whole file.

        Parameters:
            name: Path relative to the current project; use WorkDatabase/ for generated artifacts.
            old_string: Exact text to replace. Must occur EXACTLY ONCE in the file —
                include enough surrounding context (indentation, neighboring lines) to be unique.
            new_string: Replacement text.
        """

        def replace(previous):
            if previous is None:
                raise FileNotFoundError("Use write_file to create a new file")
            count = previous.count(old_string)
            if count != 1:
                raise ValueError(f"old_string must match exactly once; found {count}")
            if old_string == new_string:
                raise ValueError("old_string and new_string are identical")
            return previous.replace(old_string, new_string, 1)

        return self._update_file(name, replace)

    def search_in_files(self, keyword: str, file_extension: str = None) -> str:
        """
        Search for a keyword in files.
        Parameters:
            keyword: Keyword to search for
            file_extension: Optional, limit search to specific file types, e.g., ".py", ".txt"
        """
        results = []
        try:
            for file_path in self._base_dir.rglob("*"):
                if not file_path.is_file():
                    continue
                if file_extension and file_path.suffix != file_extension:
                    continue
                try:
                    with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
                        for line_num, line in enumerate(f, 1):
                            if keyword.lower() in line.lower():
                                rel_path = file_path.relative_to(self._base_dir)
                                results.append(f"{rel_path}:{line_num}: {line.strip()}")
                except Exception:
                    continue

            if results:
                return f"Found {len(results)} matches:\n" + "\n".join(results)
            return "No matches found"
        except Exception as e:
            return f"Search error: {e}"

    def search_web(self, query: str, max_results: int = 5) -> str:
        """
        Search web pages. Returns a list of search results (title, link, summary).
        Parameters:
            query: Search keywords
            max_results: Maximum number of results to return, defaults to 5
        """
        try:
            with DDGS() as ddgs:
                results = list(
                    ddgs.text(query, max_results=max_results, region="cn-zh")
                )

            if not results:
                logger.warning("⚠️ 没有找到相关搜索结果")
                return "No relevant search results found."

            output = []
            for i, result in enumerate(results, 1):
                title = result.get("title", "No title")
                link = result.get("href", "No link")
                snippet = result.get("body", "No summary")
                output.append(f"{i}. {title}\n   Link: {link}\n   Summary: {snippet}\n")

            result_text = "\n".join(output)
            return result_text
        except Exception as e:
            logger.error(f"❌ 搜索出错: {e}")
            return f"Error during search: {e}"

    async def run_command(self, command: str, timeout: int = 60) -> str:
        """
        Execute a Shell/terminal command.
        Python and pip commands automatically prepare/reuse the configured project
        environment; bare python/pip use it. A missing environment before the first
        command is expected. Run the requested command directly: no manual venv
        activation, framework-source inspection or host installation is needed.
        Parameters:
            command: Command to execute
            timeout: Timeout in seconds, defaults to 60
        """
        is_safe, reason = self._is_command_safe(command)
        if not is_safe:
            return f"Error: Security check rejected the command: {reason}"

        danger = self._command_needs_confirm(command)
        if danger:
            answer = (
                (
                    await self.ask_user(
                        f"⚠ 该命令将递归删除文件:\n{command}\n确认执行？(y/N)"
                    )
                )
                .strip()
                .lower()
            )
            if answer not in ("y", "yes", "是", "确认"):
                return f"已取消执行（用户未确认）: {danger}"

        try:
            async with self._command_lock:
                policy = get_agent_run_policy()
                timeout = policy.clamp_command_timeout(timeout)
                use_shell = any(
                    c in command for c in ["|", ">", "<", "&&", "||", ";", "*", "?"]
                )
                cwd = str(self._base_dir.resolve())
                overrides = None
                if re.search(r"\bclawhub\b", command, re.I):
                    cwd = str(self._clawhub_cwd)
                    if "--workdir" not in command:
                        overrides = {"CLAWHUB_WORKDIR": cwd}

                shell = use_shell or _platform.system() == "Windows"
                args = command if shell else shlex.split(command)
                result = await run_subprocess(
                    args,
                    shell=shell,
                    cwd=cwd,
                    env=overrides,
                    timeout=timeout,
                    workspace=self.workspace,
                )
                return result.to_text()
        except subprocess.TimeoutExpired:
            return f"Error: Command execution timed out ({timeout} seconds)"
        except Exception as e:
            return f"Error executing command: {e}"

    def execution_environment(self) -> str:
        """Report the configured project interpreter and cache paths."""
        return describe_execution_environment(
            cwd=self._base_dir,
            workspace=self.workspace,
        )

    async def read_image(self, image_path: str) -> ToolReturn | str:
        """Read an original image from the current project or an explicitly requested HTTP(S) URL.

        The registered immutable reference can be consumed again by memory perception.
        """
        from redlotus.ModelGateway.input_policy import ModelInputPolicy

        try:
            policy = ModelInputPolicy.for_role()
            if image_path.startswith(("http://", "https://")):
                reference = await self._references.import_url(image_path, policy=policy)
            else:
                path = self._readable_path(image_path)
                reference = await self._references.import_file(path, policy=policy)
            if not reference.media_type.startswith("image/"):
                return "Error: The supplied reference is not an image."
            return ToolReturn(
                return_value=f"Image reference: {reference.name} ({reference.id})",
                content=reference.to_prompt(),
            )
        except Exception as exc:
            return f"Error reading image: {exc}"

    async def generate_image(
        self,
        prompt: str,
        width: int = 1024,
        height: int = 1024,
        max_wait_time: int = 300,
    ) -> ToolReturn | str:
        """
        Generate images using AI model. Use this tool whenever the user asks to create, generate, make, or produce an image, picture, photo, illustration, artwork, or visual content.

        This is the PRIMARY tool for ALL image generation requests. Keywords that should trigger this tool:
        - "create image" / "generate image" / "make a picture" / "draw" / "paint" / "illustrate"
        - "give me an image" / "produce image"
        - Any request involving creating visual content, artwork, diagrams, or images (any language)

        Parameters:
            prompt: The text description of what image to generate. Be detailed and specific about the visual content, style, composition, colors, mood, etc. This is the most important parameter.
            width: Image width in pixels. Default: 1024. Common values: 512, 768, 1024, 1536, etc.
            height: Image height in pixels. Default: 1024. Common values: 512, 768, 1024, 1536, etc.
            max_wait_time: Maximum wait time in seconds. Default: 300 (5 minutes).

        Returns:
            Success: The generated image displayed inline plus generation details.
            Failure: Returns an error message.
        """
        result = await generate_image_from_flux(
            prompt, width=width, height=height, max_wait_time=max_wait_time
        )
        if not isinstance(result, tuple):
            return result

        image_bytes, mime_type, info_text = result
        ext = mimetypes.guess_extension(mime_type) or ".jpg"
        from uuid import uuid4

        name = f"generated_{uuid4().hex}{ext}"
        try:
            path = self._artifact_dir / name
            path.parent.mkdir(parents=True, exist_ok=True)
            await asyncio.to_thread(path.write_bytes, image_bytes)
        except ValueError as e:
            return f"Security error: {e}"

        text = f"{info_text}\nSaved to: {path}"
        return ToolReturn(
            return_value=text,
            content=[
                info_text,
                BinaryContent(data=image_bytes, media_type=mime_type),
            ],
        )

    def worker_tool_groups(self, *, include_browser: bool) -> dict[str, list]:
        """Worker tools grouped for resident tools and deferred capabilities."""
        groups = {
            "core": [
                self.list_files,
                self.read_file,
                self.search_in_files,
                self.search_web,
                self.ask_user,
                self._references.read_reference,
            ],
            "file_mutation": [
                self.write_file,
                self.edit_file,
            ],
            "execution": [
                self.run_command,
                self.execution_environment,
            ],
            "media": [
                self.generate_image,
                self.read_image,
                self.extract_text,
            ],
            "memory": list(self._extra_worker_tools),
            "skills": list(self._skills_manager.tools),
        }
        if include_browser:
            groups["browser"] = self._browser_session.tools
        return {name: tools for name, tools in groups.items() if tools}

    def worker_tools(self, *, include_browser: bool) -> list:
        return [
            tool
            for group in self.worker_tool_groups(
                include_browser=include_browser
            ).values()
            for tool in group
        ]
