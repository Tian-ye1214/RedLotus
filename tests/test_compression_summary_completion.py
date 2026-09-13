import pytest

from redlotus.ModelGateway.ModelChecker import (
    _COMPRESS_REQUIRED_HEADINGS,
    CompressionValidationError,
    _lint_compression_summary,
)


def test_truncated_next_action_is_not_a_valid_checkpoint():
    sections = [
        heading + "\n有效检查点内容" for heading in _COMPRESS_REQUIRED_HEADINGS[:-1]
    ]
    sections.append(_COMPRESS_REQUIRED_HEADINGS[-1] + "\n\n-")
    with pytest.raises(CompressionValidationError, match="恢复后下一步"):
        _lint_compression_summary("\n\n".join(sections))
