from redlotus.prompts import prompt
from redlotus.tools.registry import SkillsManager


def test_role_instructions_do_not_change_when_only_the_clock_changes(monkeypatch):
    skills = SkillsManager()
    monkeypatch.setattr(
        prompt, "format_prompt_current_time", lambda: "2026-09-13 10:00:00"
    )
    first = prompt.get_coordinator_system_prompt(skills, "偏好：先核对证据。")
    monkeypatch.setattr(
        prompt, "format_prompt_current_time", lambda: "2026-09-13 10:00:17"
    )
    second = prompt.get_coordinator_system_prompt(skills, "偏好：先核对证据。")
    assert first == second
    assert "偏好：先核对证据。" in second


def test_latest_time_is_separate_from_the_original_user_request(monkeypatch):
    from redlotus.tools.interaction import UserMessage

    monkeypatch.setattr(
        prompt, "format_prompt_current_time", lambda: "2026-09-13 10:00:17"
    )
    message = UserMessage(text="记住：回答简洁。")
    sent = message.to_prompt()
    assert isinstance(sent, list)
    assert sent[0] == "记住：回答简洁。"
    runtime = [
        item
        for item in sent
        if getattr(item, "metadata", {}).get("origin") == "runtime_context"
    ]
    assert len(runtime) == 1 and "2026-09-13 10:00:17" in runtime[0].content
    assert message.text == "记住：回答简洁。"
