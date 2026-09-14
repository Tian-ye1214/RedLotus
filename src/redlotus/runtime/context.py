from __future__ import annotations

import hashlib
import os
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class WorkspaceContext:
    """Immutable project identity carried into every run and child thread."""

    root: Path
    project_id: str

    @classmethod
    def from_path(cls, path: Path | str) -> WorkspaceContext:
        root = Path(path).expanduser().resolve()
        identity = os.path.normcase(str(root)).encode("utf-8")
        return cls(root, hashlib.sha256(identity).hexdigest()[:24])


_workspace_context: ContextVar[WorkspaceContext | None] = ContextVar(
    "workspace_context", default=None
)
_execution_role: ContextVar[str | None] = ContextVar("execution_role", default=None)


def current_execution_role() -> str | None:
    return _execution_role.get()


@contextmanager
def execution_role(role: str):
    token = _execution_role.set(role)
    try:
        yield
    finally:
        _execution_role.reset(token)


def active_workspace() -> WorkspaceContext | None:
    return _workspace_context.get()


@contextmanager
def workspace_context(workspace: WorkspaceContext):
    token = _workspace_context.set(workspace)
    try:
        yield workspace
    finally:
        _workspace_context.reset(token)
