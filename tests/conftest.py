"""Auxiliary regressions use only explicit temporary data and synthetic settings."""



import pytest


@pytest.fixture(autouse=True)
def isolated_config(tmp_path, monkeypatch):
    from redlotus.runtime import config

    for name, path in {
        "REDLOTUS_CONFIG_FILE": tmp_path / "config.json",
        "REDLOTUS_DOTENV_FILE": tmp_path / ".env",
        "REDLOTUS_CONFIG_DIR": tmp_path / "global",
        "REDLOTUS_DATA_DIR": tmp_path / "data",
    }.items():
        monkeypatch.setenv(name, str(path))
    monkeypatch.setattr(config, "_CONFIG", None)
