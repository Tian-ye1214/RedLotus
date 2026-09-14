"""Control results are evidence, not new user turns or invented tool calls."""

import json

from pydantic_ai.messages import (
    ModelMessagesTypeAdapter,
    ModelRequest,
    TextContent,
    UserPromptPart,
)

from redlotus.agent_core.system import AgentSystem
from redlotus.references.store import ReferenceStore
from redlotus.runtime.context import WorkspaceContext
from redlotus.tools.memory.evidence import EvidenceReader
from redlotus.tools.memory.models import ObservedTurn


async def test_control_receipt_keeps_actual_status_without_user_input(tmp_path):
    system = AgentSystem(workspace=WorkspaceContext.from_path(tmp_path))
    receipt = system.record_control_result(
        "cancel", "finished-worker", "completed", accepted=False
    )
    notices = system._session.take_notices()
    assert receipt["status"] == "completed" and not receipt["accepted"]
    assert system._session.user_inputs == []
    assert notices[0][0].metadata["origin"] == "runtime_control"
    assert system._session.take_notices() == []
    await system._toolkit.close()
    await system._memory.close()


async def test_control_receipt_is_preserved_as_non_user_evidence(tmp_path):
    workspace = WorkspaceContext.from_path(tmp_path)
    message = ModelRequest(
        parts=[
            UserPromptPart(
                [
                    TextContent(
                        "actual status: completed",
                        metadata={"origin": "runtime_control"},
                    )
                ]
            )
        ]
    )
    raw = ModelMessagesTypeAdapter.dump_python([message], mode="json")[0]
    journal = tmp_path / "main.jsonl"
    journal.write_text(
        json.dumps(
            dict(
                event_id="notice",
                meta=dict(agent="coordinator", turn_id="turn"),
                message=raw,
            )
        ),
        encoding="utf-8",
    )
    event = ObservedTurn(
        id="event",
        project_id=workspace.project_id,
        session_id="session",
        turn_id="turn",
        user_inputs=["What happened?"],
        evidence_paths=[str(journal)],
    )
    packets, sources, _ = await EvidenceReader(ReferenceStore(workspace)).collect(
        [event]
    )
    assert packets[0]["operations"][0]["kind"] == "control-return"
    assert sources["event:u0"]["text"] == "What happened?"
