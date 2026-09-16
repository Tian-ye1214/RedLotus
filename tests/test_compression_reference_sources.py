from pydantic_ai.messages import ModelRequest, UserPromptPart

from redlotus.prompts.message_text import pydantic_messages_to_text


def test_checkpoint_uses_reference_provenance_without_recopying_original_body():
    original = "预算上限3500，只核对参考资料"
    header = "【引用文件 abc】名称：large.csv；读取与计算用不可变快照：E:/references/source.csv。"
    body = "【引用文件 large.csv / CSV 行列】\n" + "123,456,789\n" * 1000
    messages = [ModelRequest(parts=[UserPromptPart([original, header, body])])]
    compact = pydantic_messages_to_text(messages, include_reference_content=False)
    assert original in compact and "E:/references/source.csv" in compact
    assert "large.csv / CSV 行列" in compact
    assert "123,456,789" not in compact
    assert "123,456,789" in pydantic_messages_to_text(messages)


def test_user_text_resembling_a_reference_label_is_not_removed():
    text = "【引用文件 请注意】\n这是我本人要求保留的任务目标。"
    messages = [ModelRequest(parts=[UserPromptPart([text])])]
    assert text in pydantic_messages_to_text(messages, include_reference_content=False)
