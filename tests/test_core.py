"""Storage/context fault checks; live model acceptance is recorded separately."""

import pytest
from pydantic_ai.messages import ModelRequest, ModelResponse, TextPart, ToolSearchCallPart
from pydantic_ai.usage import RequestUsage


def test_turn_counts_survive_replay_and_reload(tmp_path):
    from redlotus.core.agents import WorkspaceContext
    from redlotus.core.cli_commands import list_workspace_snapshots
    from redlotus.core.session import SessionFile
    from redlotus.memory import records

    (tmp_path / "config.json").write_text(
        '{"memory_perception":{"window_turns":20,"overlap_turns":3},'
        '"storage":{"project_dir":".redlotus"}}', encoding="utf-8",
    )
    workspace = WorkspaceContext.from_path(tmp_path)
    root = tmp_path / "sessions"
    session = SessionFile.create(root, workspace.project_id)
    store = records.ObservationStore(workspace)
    store.bind(session)
    for number in range(1, 4):
        event = store.begin(session.session_id, str(number), "fixture", [])
        store.finish(event)
        store.finish(event)
        assert session.completed_turns == number
        assert list_workspace_snapshots(root=root)[0].completed_turns == number
        session = SessionFile.load(session.path)
        store.bind(session)
    assert [row["number"] for row in session.pending_turns(0)] == [1, 2, 3]


@pytest.mark.parametrize("receipt", [
    ModelResponse([TextPart("cancelled")], metadata={"origin": "execution_status"}),
    ModelResponse([ToolSearchCallPart(tool_name="search", tool_call_id="auto_load_fixture")]),
])
def test_local_receipts_do_not_hide_real_model_usage(receipt):
    from redlotus.core.history import latest_usage_input_tokens, summarize_messages

    response = ModelResponse(
        [TextPart("done")], model_name="fixture", provider_name="fixture",
        usage=RequestUsage(input_tokens=950000, output_tokens=2),
    )
    messages = [response, ModelRequest([]), receipt]
    assert latest_usage_input_tokens(messages) == 950000
    summary = summarize_messages(messages, price_resolver=lambda model: None)
    assert summary.totals.responses == 1
    assert summary.totals.missing_usage_responses == 0


def test_provider_missing_usage_is_still_unknown():
    from redlotus.core.history import latest_usage_input_tokens, summarize_messages

    messages = [
        ModelResponse([TextPart("known")], usage=RequestUsage(input_tokens=950000)),
        ModelResponse([TextPart("unknown")], model_name="fixture", provider_name="fixture"),
    ]
    assert latest_usage_input_tokens(messages) is None
    assert summarize_messages(messages, price_resolver=lambda model: None).totals.missing_usage_responses == 1
