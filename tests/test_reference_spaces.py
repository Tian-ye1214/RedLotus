import pytest

from redlotus.cli.file_ref import parse_file_paths


@pytest.mark.parametrize("absolute", [False, True])
def test_unquoted_space_path_followed_by_prose_and_reference(tmp_path, absolute):
    folder = tmp_path / "模拟资料"
    folder.mkdir()
    target = folder / "方案模拟 01.csv"
    other = tmp_path / "说明.md"
    target.write_text("item,value\nA,1", encoding="utf-8")
    other.write_text("说明", encoding="utf-8")
    value = target if absolute else target.relative_to(tmp_path)
    assert parse_file_paths(f"分析 @{value}，结合@说明.md。", root=tmp_path) == [
        target,
        other,
    ]


def test_ambiguous_space_path_lists_existing_candidates(tmp_path):
    for name in ("预算.csv", "预算.csv 新版.csv"):
        (tmp_path / name).write_text("a,b", encoding="utf-8")
    with pytest.raises(ValueError, match="歧义") as error:
        parse_file_paths("看看 @预算.csv 新版.csv", root=tmp_path)
    assert "预算.csv 新版.csv" in str(error.value)


def test_quoted_space_path_disambiguates(tmp_path):
    for name in ("预算.csv", "预算.csv 新版.csv"):
        (tmp_path / name).write_text("a,b", encoding="utf-8")
    target = tmp_path / "预算.csv 新版.csv"
    assert parse_file_paths(f'@"{target.name}"', root=tmp_path) == [target]
