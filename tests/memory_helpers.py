"""Bound-session fixtures; they never stand in for real model production."""

from redlotus.core.session import SessionFile
from redlotus.memory.records import ObservationStore
from redlotus.memory.service import MemoryService


def session_file(workspace, identity="session"):
    root = workspace.root / "test-sessions"
    path = root / identity / "model_messages.json"
    if path.exists():
        return SessionFile.load(path)
    return SessionFile.create(root, workspace.project_id, session_id=identity)


def new_memory(*, workspace, **kwargs):
    memory = MemoryService(workspace=workspace, **kwargs)
    memory.bind_session(session_file(workspace))
    return memory


def bound_observations(workspace, **kwargs):
    store = ObservationStore(workspace, **kwargs)
    store.bind(session_file(workspace))
    return store
