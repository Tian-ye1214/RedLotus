"""Pre-operation file authorization shared by tools and command execution."""

from __future__ import annotations

import asyncio
import difflib
import hashlib
import os
import re
import shlex
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path


class FileOperation(StrEnum):
    WRITE = "写入"
    DELETE = "删除"


@dataclass(frozen=True)
class FileMutation:
    name: str
    path: Path
    operation: FileOperation
    fingerprint: str
    scope: object
    before: str | None = None
    after: str | None = None
    recursive: bool = False

    @property
    def preview(self) -> str:
        if self.operation == FileOperation.DELETE:
            return "删除整个目录及其所有内容" if self.recursive else "删除该文件"
        if self.before is None and not self.after:
            return "将新建空文件"
        return "".join(difflib.unified_diff(
            (self.before or "").splitlines(keepends=True), (self.after or "").splitlines(keepends=True),
            fromfile=str(self.path), tofile=str(self.path))) or "内容不变"


class FileAccessPolicy:
    """Authorize a concrete change, then reject changed targets or expired sessions."""

    def __init__(self, workspace: Path, resolve, ask):
        self.workspace, self.resolve, self.ask = workspace, resolve, ask
        self.scope = lambda: None
        self.approvals: set[FileMutation] = set()

    def internal(self, path: Path) -> bool:
        # A redirected WorkDatabase is external; resolve the target, not this boundary.
        return path.resolve().is_relative_to(self.workspace.resolve() / "WorkDatabase")

    def fingerprint(self, path: Path) -> str:
        digest = hashlib.sha256()
        if not path.exists():
            return "missing"
        paths = [path, *sorted(path.rglob("*"))] if path.is_dir() else [path]
        for entry in paths:
            info = entry.lstat()
            digest.update(f"{entry}:{info.st_ino}:{info.st_size}:{info.st_mtime_ns}:{info.st_mode}".encode())
            if entry.is_symlink() or entry.is_junction():
                digest.update(str(entry.resolve()).encode())
            elif entry.is_file():
                with entry.open("rb") as stream:
                    for block in iter(lambda: stream.read(1024 * 1024), b""):
                        digest.update(block)
        return digest.hexdigest()

    def plan_write(self, name: str, update) -> FileMutation:
        path = self.resolve(name)
        fingerprint = self.fingerprint(path)
        before = path.read_bytes().decode("utf-8") if path.exists() else None
        proposal = FileMutation(name, path, FileOperation.WRITE, fingerprint, self.scope(), before, update(before))
        self.verify(proposal)
        return proposal

    def plan_delete(self, name: str, recursive: bool) -> FileMutation:
        path = self.resolve(name)
        if not path.exists():
            raise FileNotFoundError(path)
        if path.is_dir() and not recursive:
            raise ValueError("删除目录需要 recursive=True，并确认完整删除范围。")
        if path == Path(path.anchor) or path == self.workspace.resolve():
            raise ValueError("不能通过 delete_file 删除磁盘根或整个项目。")
        return FileMutation(name, path, FileOperation.DELETE, self.fingerprint(path), self.scope(), recursive=recursive)

    def plan_output(self, name: str, data: bytes) -> FileMutation:
        path = self.resolve(name)
        fingerprint = self.fingerprint(path)
        before = f"覆盖已有文件：{path.stat().st_size} 字节，版本校验 {fingerprint}\n" if path.exists() else None
        summary = f"二进制文件：{len(data)} 字节，SHA256 {hashlib.sha256(data).hexdigest()}\n"
        return FileMutation(name, path, FileOperation.WRITE, fingerprint, self.scope(), before=before, after=summary)

    def commit_output(self, proposal: FileMutation, data: bytes) -> None:
        from redlotus.runtime.resources import atomic_write
        self.verify(proposal)
        atomic_write(proposal.path, data)

    async def confirm(self, question: str, scope) -> None:
        answer = await self.ask(question)
        text = getattr(answer, "text", getattr(answer, "return_value", answer))
        if str(text).strip().lower() not in {"y", "yes", "是", "确认", "同意"}:
            raise PermissionError("已取消：用户未确认该操作。")
        if self.scope() != scope or asyncio.current_task().cancelling():
            raise PermissionError("会话或任务已失效，操作未执行。")

    async def authorize(self, proposal: FileMutation) -> None:
        self.approvals.intersection_update(item for item in self.approvals.copy() if item.scope == proposal.scope)
        if not self.internal(proposal.path) and proposal not in self.approvals:
            await self.confirm(f"请求{proposal.operation.value} WorkDatabase 外的文件：\n{proposal.path}\n"
                               f"{proposal.preview}\n确认执行？(y/N)", proposal.scope)
            self.approvals.add(proposal)

    def verify(self, proposal: FileMutation) -> None:
        if self.scope() != proposal.scope:
            raise PermissionError("会话或任务已失效，操作未执行。")
        if self.resolve(proposal.name) != proposal.path or self.fingerprint(proposal.path) != proposal.fingerprint:
            raise ValueError("确认期间文件或实际路径已变化，请重新读取后发起操作。")

    async def authorize_command(self, args, cwd: str) -> None:
        """Allow narrow read commands; opaque scripts require approval of this execution."""
        scope = self.scope()
        raw = args if isinstance(args, str) else shlex.join(args)
        paths = []
        safe = not re.search(r"[;,=&|><`$()%!^*?{}\[\]\'\x00-\x1f\x7f]", raw)
        lexer = shlex.shlex(raw, posix=True)
        lexer.whitespace_split = True
        lexer.commenters = lexer.escape = ""
        try:
            values = list(lexer)
        except ValueError:
            values = []
        # Only actual CMD builtins are unshadowable. A script named rg.cmd,
        # or a PATH/current-directory executable, is an opaque execution.
        program = values[0].lower() if values and isinstance(args, str) and os.name == "nt" else ""
        if safe and program in {"dir", "type"}:
            return scope
        mutations = {"mkdir": set(), "md": set(), "rmdir": {"/s", "/q"}, "rd": {"/s", "/q"},
                     "del": {"/f", "/s", "/q"}, "erase": {"/f", "/s", "/q"},
                     "copy": {"/y", "/b"}, "move": {"/y"}}
        if safe and program in mutations:
            operands = [value for value in values[1:] if value.lower() not in mutations[program]]
            if operands and (program not in {"copy", "move"} or len(operands) == 2) and all(not value.startswith(("-", "/")) for value in operands):
                paths = [str((Path(cwd) / value).resolve()) for value in operands]
                if all(self.internal(Path(path)) for path in paths) and all(
                        not part.endswith((".", " ")) for value in operands for part in Path(value).parts):
                    return scope
        detail = "\n实际路径：\n" + "\n".join(paths) if paths else "\n写入范围无法可靠静态确定；本次执行可能修改 WorkDatabase 外的文件。"
        await self.confirm(f"请求执行命令（仅授权此次调用）：\n工作目录：{cwd}\n{raw}{detail}\n确认执行？(y/N)", scope)
        return scope

    def desktop(self) -> Path:
        """Resolve the OS desktop, including Windows Known Folder redirection."""
        if os.name == "nt":
            import winreg
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                r"Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders") as key:
                value, _ = winreg.QueryValueEx(key, "Desktop")
            return Path(os.path.expandvars(value)).resolve()
        config = Path.home() / ".config/user-dirs.dirs"
        if config.is_file():
            match = re.search(r'^XDG_DESKTOP_DIR="([^"]+)"', config.read_text(), re.M)
            if match:
                return Path(match[1].replace("$HOME", str(Path.home()))).expanduser().resolve()
        return Path.home() / "Desktop"
