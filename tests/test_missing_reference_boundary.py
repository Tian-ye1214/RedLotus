from redlotus.cli.file_ref import parse_file_paths


def test_missing_reference_stops_at_chinese_punctuation(tmp_path):
    assert parse_file_paths("看看 @不存在.md，不能读取就指出原因。", root=tmp_path) == [
        tmp_path / "不存在.md"
    ]
