"""QQ SDK startup must not persist or log the user's private configuration."""
import importlib
import logging
from types import SimpleNamespace

import pytest

from redlotus.api.QQ import QQBot


@pytest.mark.parametrize("startup_fails", [False, True])
def test_qq_startup_preserves_private_config(tmp_path, monkeypatch, startup_fails):
    path = tmp_path / "config.yaml"
    original = b"# Preserve comments and untouched settings.\nbt_uin: '123456789'\n"
    path.write_bytes(original)
    monkeypatch.setenv("NCATBOT_CONFIG_PATH", str(path))
    monkeypatch.setenv("LOG_FILE_PATH", str(tmp_path / "logs"))
    monkeypatch.setenv("LOG_FORMAT", "%(message)s")
    monkeypatch.setenv("LOG_FILE_FORMAT", "%(message)s")

    import ncatbot.core
    import ncatbot.utils
    sdk = importlib.import_module("ncatbot.utils.config")
    config = sdk.Config(bt_uin="123456789", root="42",
        napcat=sdk.NapCatConfig(ws_token="Synthetic_ws_123!", webui_token="Synthetic_ui_456!", enable_webui=False),
        plugin=sdk.PluginConfig(plugins_dir=str(tmp_path / "plugins")))
    monkeypatch.setattr(ncatbot.utils, "config", config)
    monkeypatch.setattr(sdk, "CONFIG_PATH", str(path))
    records = []

    class Capture(logging.Handler):
        def emit(self, record):
            records.append(record.getMessage())

    config_log = logging.getLogger("Config")
    monkeypatch.setattr(config_log, "handlers", [Capture()])
    monkeypatch.setattr(config_log, "filters", [])
    monkeypatch.setattr(config_log, "propagate", False)

    class Client:
        def __init__(self):
            from ncatbot.core.api.api import BotAPI
            self.api = BotAPI(None)
            self.adapter = SimpleNamespace(connect_websocket=None)

        def add_private_message_handler(self, handler):
            pass

        def add_group_message_handler(self, handler):
            pass

        def run_frontend(self, **kwargs):
            config.validate_config()
            if startup_fails:
                raise ConnectionError("synthetic startup failure")

    monkeypatch.setattr(ncatbot.core, "BotClient", Client)
    bot = QQBot()
    saved_method = config.save
    if startup_fails:
        with pytest.raises(ConnectionError, match="synthetic startup failure"):
            bot.run()
    else:
        bot.run()
    assert path.read_bytes() == original
    assert config.save == saved_method
    assert any("QQ SDK 配置已校验" in message for message in records)
    assert not any(config.napcat.ws_token in message or config.napcat.webui_token in message
        for message in records)
    assert bot._released
