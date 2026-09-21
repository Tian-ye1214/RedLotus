"""Auxiliary fault checks never use the owner's configuration or saved sessions."""

import pytest


@pytest.fixture(autouse=True)
def isolated_paths(tmp_path, monkeypatch):
    for name, path in {
        "REDLOTUS_CONFIG_FILE": tmp_path / "config.json",
        "REDLOTUS_DOTENV_FILE": tmp_path / ".env",
        "REDLOTUS_CONFIG_DIR": tmp_path / "global",
        "REDLOTUS_DATA_DIR": tmp_path / "data",
    }.items():
        monkeypatch.setenv(name, str(path))

