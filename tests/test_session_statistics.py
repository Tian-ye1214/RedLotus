"""Panels retain session identity and role counters after transcript pruning."""

from datetime import timedelta

from pydantic_ai.messages import ModelResponse, TextPart
from pydantic_ai.usage import RequestUsage

from redlotus.core.presentation import PanelSnapshotCache, _collect_history
from redlotus.core.session import SessionFile


def test_same_title_sessions_and_child_usage_remain_separate(tmp_path):
    for identity in ("one", "two"):
        session = SessionFile.create(tmp_path, "project", session_id=identity, title="same title")
        main = ModelResponse(parts=[TextPart("main")], usage=RequestUsage(input_tokens=100))
        child = ModelResponse(parts=[TextPart("child")], usage=RequestUsage(input_tokens=50))
        session.save_context([main], turn_id=identity)
        session.record_usage([child], role="worker", invocation=identity)
        session.save_context([], turn_id=identity)
        session.compact(keep_turn_ids=set())

    history, sessions = _collect_history(tmp_path, PanelSnapshotCache())
    assert history.conversation_count == 2
    assert history.responses == 4
    assert history.by_agent["worker"].input_tokens == 100
    assert history.by_agent["coordinator"].input_tokens == 200
    assert all(session.agents == {"coordinator", "worker"} for session in sessions)
    assert all(session.responses == 2 for session in sessions)


def test_child_control_receipts_do_not_become_billable_model_responses(tmp_path):
    session = SessionFile.create(tmp_path, "project")
    response = ModelResponse(
        parts=[TextPart("model result")], usage=RequestUsage(input_tokens=50),
    )
    receipt = ModelResponse(
        parts=[TextPart("process cancelled")], metadata={"origin": "execution_status"},
        timestamp=response.timestamp + timedelta(seconds=1),
    )
    session.record_usage([response, receipt], role="worker", invocation="child")
    session.save_context([receipt], turn_id="main-control")
    history, _ = _collect_history(tmp_path, PanelSnapshotCache())
    assert history.responses == 1
    assert history.missing_usage_responses == 0
    assert history.by_agent["worker"].input_tokens == 50
    assert session.model_messages()[0].parts[0].content == "process cancelled"
