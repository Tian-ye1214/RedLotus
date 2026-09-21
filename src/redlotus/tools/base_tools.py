"""Common file, command, web, document and media tool implementations."""

from __future__ import annotations

import asyncio
import difflib
import inspect
import mimetypes
import platform as _platform
import re
import shlex
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import requests
from ddgs import DDGS
from pydantic_ai import BinaryContent, ToolReturn

from redlotus.runtime import logging as logger
from redlotus.runtime.config import get_env
from redlotus.runtime.resources import (
    WorkspaceContext,
    atomic_write_text,
    bind_to_loop,
    current_workspace,
    runtime_dir,
    user_skills_dir,
)
from redlotus.tools.execution import (
    PlaywrightBrowserSession,
    describe_execution_environment,
    run_subprocess,
)
from redlotus.tools.references import ReferenceStore
from redlotus.tools.registry import SkillsManager, resolve_readable_path


async def generate_image_from_flux(prompt: str, width: int = 1024, height: int = 1024, max_wait_time: int = 300):
    """
    Generate images using AI model. Use this tool whenever the user asks to create, generate, make, or produce an image, picture, photo, illustration, artwork, or visual content.

    This is the PRIMARY tool for ALL image generation requests. Keywords that should trigger this tool:
    - "create image" / "generate image" / "make a picture" / "draw" / "paint" / "illustrate"
    - "give me an image" / "produce image"
    - Any request involving creating visual content, artwork, diagrams, or images (any language)

    Args:
        prompt: The text description of what image to generate. Be detailed and specific about the visual content, style, composition, colors, mood, etc. This is the most important parameter.
        width: Image width in pixels. Default: 1024. Common values: 512, 768, 1024, 1536, etc.
        height: Image height in pixels. Default: 1024. Common values: 512, 768, 1024, 1536, etc.
        max_wait_time: Maximum wait time in seconds. Default: 300 (5 minutes).

    Returns:
        Success: Returns a tuple (image_bytes, mime_type, info_text).
        Failure: Returns an error message string.
    """
    bfl_base_url = get_env("BFL_BASE_URL", warn=False)
    bfl_api_key = get_env("BFL_API_KEY", warn=False)
    if not bfl_api_key:
        return "Error: BFL_API_KEY environment variable is not set. Please set it before using image generation."

    try:
        logger.info(f"正在提交图像生成请求: {prompt[:50]}...")
        response = await asyncio.to_thread(
            requests.post,
            bfl_base_url,
            headers={
                "accept": "application/json",
                "x-key": bfl_api_key,
                "Content-Type": "application/json",
            },
            json={
                "prompt": prompt,
                "width": width,
                "height": height,
            },
            timeout=30
        )
        response.raise_for_status()
        response_data = response.json()

        request_id = response_data.get("id")
        polling_url = response_data.get("polling_url")

        if not polling_url:
            return f"Error: No polling_url received from API. Response: {response_data}"

        logger.info(f"请求已提交，Request ID: {request_id}")
        logger.info("正在等待图像生成完成...")

        start_time = time.time()
        poll_count = 0

        while True:
            elapsed_time = time.time() - start_time
            if elapsed_time > max_wait_time:
                return f"Error: Image generation timed out after {max_wait_time} seconds. Request ID: {request_id}"

            poll_count += 1
            if poll_count % 10 == 0:
                logger.info(f"仍在等待中... (已等待 {elapsed_time:.1f} 秒)")

            result_response = await asyncio.to_thread(
                requests.get,
                polling_url,
                headers={
                    "accept": "application/json",
                    "x-key": bfl_api_key
                },
                timeout=30
            )
            result_response.raise_for_status()
            result = result_response.json()

            status = result.get("status", "Unknown")

            if status == "Ready":
                image_url = result.get("result", {}).get("sample")
                if image_url:
                    logger.info("图像生成成功！")
                    logger.info(f"图像URL: {image_url}")
                    img_response = await asyncio.to_thread(requests.get, image_url, timeout=30)
                    img_response.raise_for_status()
                    image_bytes = img_response.content
                    mime_type = img_response.headers.get("Content-Type", "image/jpeg").split(";")[0].strip()
                    info_text = f"Image generated successfully!\nImage URL: {image_url}\nPrompt: {prompt}\nDimensions: {width}x{height}"
                    return image_bytes, mime_type, info_text
                else:
                    return f"Error: Image generation completed but no image URL found in response. Response: {result}"

            elif status == "Failed":
                error_msg = result.get("error", "Unknown error")
                logger.error(f"图像生成失败: {error_msg}")
                return f"Error: Image generation failed - {error_msg}"

            await asyncio.sleep(0.5)

    except requests.exceptions.RequestException as e:
        logger.error(f"API请求错误: {e}")
        return f"Error: API request failed - {e}"
    except Exception as e:
        logger.error(f"图像生成异常: {e}")
        return f"Error: Image generation exception - {type(e).__name__}: {e}"


class BasicToolkit:
    def __init__(
        self,
        skills_manager: SkillsManager,
        *,
        workspace: WorkspaceContext | None = None,
        show_diff,
    ):
        self.workspace = workspace or WorkspaceContext.from_path(current_workspace())
        if skills_manager is not None:
            skills_manager.workspace = self.workspace
        self._clawhub_cwd = runtime_dir(self.workspace)
        self._skills_overlay = user_skills_dir(self.workspace)
        self._WORK_DATABASE_ROOT = self.workspace.root / "WorkDatabase"
        self._artifact_dir = self._WORK_DATABASE_ROOT
        self._base_dir: Path = self.workspace.root
        self._file_lock = threading.Lock()
        self._command_lock = asyncio.Lock()
        self._review_store = PendingReviewStore(self._file_lock)
        self._references = ReferenceStore(self.workspace)
        self._ask_user_handler = None
        self._show_diff = show_diff
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

        child = BasicToolkit(
            SkillsManager(workspace=self.workspace),
            workspace=self.workspace, show_diff=self._show_diff,
        )
        child._file_lock = self._file_lock
        child._review_store = self._review_store
        child._artifact_dir = self._artifact_dir
        child.set_ask_user_handler(bind_to_loop(self.ask_user, owner_loop))
        return child

    def set_task_directory(self, task_name: str) -> Path:
        """
        Set a dedicated work directory for the current task.

        Args:
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

    def _readable_path(self, name: str) -> Path:
        return resolve_readable_path(name, work_base=self._base_dir)

    def _safe_path(self, name: str) -> Path:
        path = self._readable_path(name)
        root = self.workspace.root
        if not path.is_relative_to(root):
            raise ValueError(f"Path not under current project: {path}")
        return path

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

        Args:
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
        """Parse a project document that has not already been supplied as a reference.

        Returns structured text and native media. Use supplied reference contents directly;
        read_reference(reference_id) retrieves an immutable version when it is needed again.

        Args:
            name: Document path within the project or an allowed Skill directory.

        Returns:
            The registered reference identity, parsed text and original media, or an error.
        """
        from redlotus.runtime.network import ModelInputPolicy

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
        Read the current on-disk version of a project text file.
        Use for source code, generated artifacts, changes since a reference was captured,
        or an explicit reread. Reference blocks already contain the stated snapshot content;
        use read_reference for that immutable version when it is absent from context.

        Args:
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

        Args:
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
            added, deleted, modified = self._show_diff(old, content, path=name)
            return f"Saved '{name}' ({len(content)} characters; +{added} -{deleted} ~{modified})"
        except (OSError, ValueError) as exc:
            return f"Error updating '{name}': {exc}"

    def write_file(self, name: str, content: str) -> str:
        """
        Create or overwrite a project file and display the resulting diff.

        Args:
            name: Path relative to the current project; use WorkDatabase/ for generated artifacts.
            content: Content to write
        """
        return self._update_file(name, lambda previous: content)

    def edit_file(self, name: str, old_string: str, new_string: str) -> str:
        """
        Edit an existing file by replacing an EXACT, UNIQUE snippet (string replace).
        Prefer this over write_file when modifying an existing file: it makes a precise,
        local change and shows a colored diff instead of rewriting the whole file.

        Args:
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

        Args:
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

        Args:
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

    async def run_command(self, command: str, timeout: int | None = None) -> str:
        """
        Execute a Shell/terminal command.
        Python uses the interpreter running this application, or an external PATH
        interpreter for a packaged executable. Dependency installation changes
        that existing environment; inspect execution_environment for its path.
        Use pip for package operations: its installation target is pinned to that
        interpreter even when the pip executable itself belongs to another environment.
        An explicit Python invocation with extra flags still requires its own pip module.
        No environment is created automatically. Missing Python or packages are
        reported as errors; ordinary commands do not require Python.

        Args:
            command: Command to execute
            timeout: Optional seconds, capped by the configured command limit; omitted uses that limit.
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
        except subprocess.TimeoutExpired as exc:
            return f"Error: Command execution timed out ({exc.timeout} seconds)"
        except Exception as e:
            return f"Error executing command: {e}"

    def execution_environment(self) -> str:
        """Report the existing interpreter, dependency install location and project cache paths."""
        return describe_execution_environment(
            cwd=self._base_dir,
            workspace=self.workspace,
        )

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

        Args:
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


def _opcodes(baseline: str, current: str):
    a = baseline.splitlines(keepends=True)
    b = current.splitlines(keepends=True)
    sm = difflib.SequenceMatcher(a=a, b=b, autojunk=False)
    return a, b, sm.get_opcodes()


@dataclass(frozen=True)
class Hunk:
    """一处连续改动（baseline→current 中的一个非 equal 区块）。"""

    index: int
    old_start: int
    old_lines: list[str]
    new_start: int
    new_lines: list[str]

    @property
    def location(self) -> str:
        return f"L{self.new_start}" if self.new_lines else f"L{self.old_start}"


def compute_hunks(baseline: str, current: str) -> list[Hunk]:
    """把 baseline→current 的差异切成逐块 Hunk 列表（equal 区块跳过）。"""
    a, b, ops = _opcodes(baseline, current)
    hunks: list[Hunk] = []
    for tag, i1, i2, j1, j2 in ops:
        if tag == "equal":
            continue
        hunks.append(Hunk(len(hunks), i1 + 1, a[i1:i2], j1 + 1, b[j1:j2]))
    return hunks


def reconstruct(baseline: str, current: str, rejected: set[int]) -> str:
    """按逐块决定重建文件内容：rejected 的块取 baseline 侧，其余取 current 侧。

    rejected 为空 → 完全等于 current；rejected 含全部块 → 完全等于 baseline。
    """
    a, b, ops = _opcodes(baseline, current)
    out: list[str] = []
    idx = 0
    for tag, i1, i2, j1, j2 in ops:
        if tag == "equal":
            out.extend(b[j1:j2])
            continue
        out.extend(a[i1:i2] if idx in rejected else b[j1:j2])
        idx += 1
    return "".join(out)


@dataclass
class ReviewEntry:
    path: Path
    name: str
    baseline: str
    snapshot: str
    decisions: dict[int, bool] = field(default_factory=dict)
    existed: bool = True

    @property
    def hunks(self):
        return compute_hunks(self.baseline, self.snapshot)

    def check_current(self, current):
        rejected = {key for key, value in self.decisions.items() if value}
        expected = (
            None if not self.existed and len(rejected) == len(self.hunks)
            else reconstruct(self.baseline, self.snapshot, rejected)
        )
        if current != expected:
            raise ValueError("文件已在审查界面之外被修改；为保留这些改动，本次操作未应用。")


class PendingReviewStore:
    """跨线程共享的待审查暂存区。复用 toolkit 的 file_lock，避免与 agent 写盘竞争。"""

    def __init__(self, file_lock: threading.Lock) -> None:
        self._lock = file_lock
        self._entries: dict[str, ReviewEntry] = {}
        self._on_change: Callable[[], None] | None = None

    def activate(self, on_change: Callable[[], None]) -> None:
        with self._lock:
            self._on_change = on_change

    def deactivate(self) -> None:
        with self._lock:
            self._on_change = None
            self._entries.clear()

    def clear(self) -> None:
        """Discard the previous project's reviews while retaining the UI subscription."""
        with self._lock:
            self._entries.clear()
            cb = self._on_change
        self._notify(cb)

    def write(self, path: Path, name: str, update):
        """Publish file contents and their review snapshot as one locked operation."""
        with self._lock:
            previous = path.read_text(encoding="utf-8") if path.exists() else None
            old = self._entries.get(str(path))
            if old:
                old.check_current(previous)
            content = update(previous)
            baseline = reconstruct(
                old.baseline, old.snapshot,
                {hunk.index for hunk in old.hunks if old.decisions.get(hunk.index) is not False},
            ) if old else previous or ""
            existed = old.existed or False in old.decisions.values() if old else previous is not None
            atomic_write_text(path, content)
            if self._on_change is not None and baseline != content:
                self._entries[str(path)] = ReviewEntry(path, name, baseline, content, existed=existed)
            else:
                self._entries.pop(str(path), None)
            callback = self._on_change
        self._notify(callback)
        return previous or "", content

    def entries(self) -> list[ReviewEntry]:
        with self._lock:
            return list(self._entries.values())

    def get(self, key: str) -> ReviewEntry | None:
        with self._lock:
            return self._entries.get(key)

    def decide(self, entry: ReviewEntry, index: int, reject: bool) -> bool:
        """Apply a decision only to the exact version displayed by the UI."""
        with self._lock:
            if self._entries.get(str(entry.path)) is not entry:
                return False
            entry.check_current(entry.path.read_text(encoding="utf-8") if entry.path.exists() else None)
            decisions = {**entry.decisions, index: reject}
            rejected = {key for key, value in decisions.items() if value}
            if not entry.existed and len(rejected) == len(entry.hunks):
                entry.path.unlink(missing_ok=True)
            else:
                atomic_write_text(
                    entry.path,
                    reconstruct(entry.baseline, entry.snapshot, rejected),
                )
            entry.decisions = decisions
        return True

    def finish_decided(self):
        for entry in self.entries():
            if all(hunk.index in entry.decisions for hunk in entry.hunks):
                self.finish(str(entry.path))

    def finish(self, key: str) -> None:
        with self._lock:
            self._entries.pop(key, None)
            cb = self._on_change
        self._notify(cb)

    def _notify(self, cb: Callable[[], None] | None) -> None:
        if cb is not None:
            cb()
