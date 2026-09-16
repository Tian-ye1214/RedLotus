"""Verify unchanged visible output while presentation implementations are shared."""

import hashlib
from types import SimpleNamespace

import pytest

from redlotus.core import presentation as cli_ui


@pytest.mark.parametrize("encoding, expected", [
    ("utf-8", "ba6b105ad69daa78ea2a61f891e7c39e1dbdfa4f0190d9ac71918bf89b532111"),
    ("gbk", "ecd46a05fd1af53caf42d72e894d7fd824d8ce351d71ebb14d9646fef63dec9e"),
])
def test_startup_logo_matches_pre_refactor_rendering(monkeypatch, encoding, expected):
    output = []
    monkeypatch.setattr(cli_ui, "sys", SimpleNamespace(stdout=SimpleNamespace(encoding=encoding)))
    monkeypatch.setattr(cli_ui, "emit_renderable", output.append)
    cli_ui.print_startup_logo()
    assert hashlib.sha256("\n".join(output).encode()).hexdigest() == expected
