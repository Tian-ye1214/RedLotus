import inspect
import os
import re
import sys
from functools import partial
from pathlib import Path

# ncatbot 在导入时即冻结配置（从 NCATBOT_CONFIG_PATH 读取配置文件路径），
# 故须在导入 ncatbot 之前指向 config.yaml（用户配置目录，首次从随包模板 seed），否则会回退到 input() 阻塞。
from redlotus.core.config import resource_root, user_config_dir


def _ensure_bot_config_path() -> Path:
    cfg = user_config_dir() / "config.yaml"
    if not cfg.exists():
        tmpl = resource_root() / "api" / "config.yaml.example"
        try:
            cfg.parent.mkdir(parents=True, exist_ok=True)
            if tmpl.is_file():
                cfg.write_text(tmpl.read_text(encoding="utf-8"), encoding="utf-8")
        except OSError:
            return tmpl
    return cfg


os.environ.setdefault("NCATBOT_CONFIG_PATH", str(_ensure_bot_config_path()))

from ncatbot.core import BaseMessageEvent, BotClient, GroupMessageEvent, MetaEvent
from ncatbot.plugin_system import on_message
from ncatbot.utils import config
from ncatbot.utils.config import strong_password_check

from redlotus.core import config as logger
from redlotus.core.config import get_env
from redlotus.tools.interaction import UserMessage
from redlotus.api.base import BotBase
from redlotus.api.qq_media_helpers import extract_media


class QQBot(BotBase):
    _ENV_AGENT_TIMEOUT = "QQ_AGENT_TIMEOUT_S"
    _ENV_SEND_TIMEOUT = "QQ_SEND_REPLY_TIMEOUT_S"
    _FILE_ALLOW_EXT = frozenset(
        {
            ".png",
            ".jpg",
            ".jpeg",
            ".gif",
            ".webp",
            ".bmp",
            ".mp4",
            ".mov",
            ".mkv",
            ".webm",
            ".pdf",
            ".txt",
            ".md",
            ".docx",
            ".doc",
            ".xlsx",
            ".xls",
            ".pptx",
            ".ppt",
            ".csv",
            ".json",
            ".html",
        }
    )

    def __init__(self):
        super().__init__()
        if uin := get_env("QQBOT_ID", warn=False):
            config.set_bot_uin(uin)
        self._bot_client = BotClient()
        self._bot_client.add_shutdown_handler(self._on_shutdown)

    platform_tag = "QQ"
    session_prefix = "qq_"

    def clean_text(self, raw: str) -> str:
        return re.sub(r"\[CQ:[^\]]+\]", "", raw or "").strip()

    def _is_at_me(self, event: BaseMessageEvent) -> bool:
        if not isinstance(event, GroupMessageEvent):
            return True
        msg = getattr(event, "message", None)
        return msg is not None and msg.is_user_at(config.bt_uin)

    def _session_id(self, event: BaseMessageEvent) -> str:
        if isinstance(event, GroupMessageEvent):
            return f"group_{event.group_id}"
        return f"private_{event.user_id}"

    async def _reply_event(self, event: BaseMessageEvent, text: str) -> None:
        sig = inspect.signature(event.reply)
        await (
            event.reply(text=text, at=False)
            if "at" in sig.parameters
            else event.reply(text=text)
        )

    async def _extract_attachments(self, event: BaseMessageEvent) -> list:
        return await extract_media(self._bot_client.api, event, self._FILE_ALLOW_EXT)

    async def _on_shutdown(self, _: MetaEvent) -> None:
        await self.release_all_resources_async()

    async def _handle_message(self, event: BaseMessageEvent) -> None:
        raw_text = (event.raw_message or "").strip()
        if not raw_text or (
            isinstance(event, GroupMessageEvent) and not self._is_at_me(event)
        ):
            return
        session_id = self._session_id(event)
        user_text = self.clean_text(raw_text)
        attachments = await self._extract_attachments(event)
        await self.dispatch_user_message(
            session_id,
            UserMessage(
                text=user_text,
                attachments=attachments,
                original_text=self.clean_text(raw_text),
            ),
            partial(self._reply_event, event),
        )

    def _register_handlers(self) -> None:
        on_message(self._handle_message)

    def _doctor(self) -> None:
        """启动前体检：配置缺失/无效时立即报错退出，避免 ncatbot 回退到 input() 静默卡死。"""
        config_path = Path(os.environ["NCATBOT_CONFIG_PATH"])
        if not config_path.is_file():
            logger.error(
                f"[QQ] 找不到 NapCat 配置文件 {config_path}。"
                f"请在 {config_path.parent} 下放置 config.yaml（参照随包模板）。"
            )
            sys.exit(1)
        uin = str(config.bt_uin or "")
        if uin in ("", "None", "123456"):
            logger.error(
                "[QQ] 机器人 QQ 号未配置。请设置环境变量 QQBOT_ID，"
                f"或在 {config_path} 中填写 bt_uin。"
            )
            sys.exit(1)
        token = config.napcat.webui_token
        if not strong_password_check(token):
            logger.error(
                f"[QQ] NapCat WebUI 令牌强度不足（{config_path} 的 napcat.webui_token）。"
                f"请改为至少 12 位、含数字与大小写字母及特殊符号的强密码。"
            )
            sys.exit(1)

    def run(
        self,
        *,
        debug: bool = True,
        remote_mode: bool = True,
        enable_webui_interaction: bool = False,
        **kwargs,
    ):
        self._doctor()
        self._released = False
        self._register_handlers()
        try:
            self._bot_client.run_frontend(
                debug=debug,
                remote_mode=remote_mode,
                enable_webui_interaction=enable_webui_interaction,
                **kwargs,
            )
        finally:
            self.release_all_resources()


if __name__ == "__main__":
    QQBot().run()
