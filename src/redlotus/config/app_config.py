from __future__ import annotations

import json
import os
import hashlib
from copy import deepcopy
from typing import TYPE_CHECKING, Any

from redlotus.infra.persist_utils import save_locked_json, file_lock, atomic_write_json

if TYPE_CHECKING:
    from pydantic_ai.usage import UsageLimits as _UsageLimits

from redlotus.infra import logger
from dotenv import dotenv_values
from redlotus.infra.paths import config_file, default_config_file, dotenv_file
from pydantic_ai.usage import UsageLimits
from redlotus.runtime.runtime_state import AgentRunPolicy

_CONFIG: tuple[tuple, dict[str, Any]] | None = None
_DOTENV_CACHE: dict[tuple, dict[str, str]] | None = None
_API_CONFIG_KEYS = {"BASE_URL", "API_KEY", "SILICONFLOW_BASE", "SILICONFLOW_KEY"}


def _copy_missing_defaults(config, defaults, section=""):
    for key, value in defaults.items():
        if key not in config:
            config[key] = deepcopy(value)
        elif (
            isinstance(value, dict)
            and isinstance(config[key], dict)
            and section != "models"
            and key not in ("gateways", "model_presets")
        ):
            if key == "context":
                contexts = config[key]
                roles = set(defaults["models"]) & set(value)
                if (
                    not roles.intersection(contexts)
                    and "default_context_tokens" in contexts
                ):
                    _copy_missing_defaults(contexts, value["coordinator"])
                    _copy_missing_defaults(
                        contexts, {"compression": value["compression"]}
                    )
                    continue
                value = deepcopy(value)
                for role in roles:
                    value[role] = {
                        k: v
                        for k, v in value[role].items()
                        if k not in contexts.get("defaults", {})
                    }
            _copy_missing_defaults(config[key], value, key)


def initialize_config() -> None:
    path = config_file()
    with file_lock(path):
        defaults = json.loads(default_config_file().read_text(encoding="utf-8"))
        if not path.exists():
            atomic_write_json(path, defaults)
            return
        raw = path.read_bytes()
        current = json.loads(raw)
        updated = deepcopy(current)
        _copy_missing_defaults(updated, defaults)
        execution = updated["execution"]
        if "blocked_code_patterns" in execution["permissions"]:
            execution["permissions"].pop("blocked_code_patterns")
            execution["inherit_env"] = list(
                dict.fromkeys(
                    [
                        *execution["inherit_env"],
                        *defaults["execution"]["inherit_env"],
                    ]
                )
            )
        if updated != current:
            backup = (
                path.parent
                / "config-backups"
                / (hashlib.sha256(raw).hexdigest() + ".json")
            )
            if not backup.exists():
                atomic_write_json(backup, current)
            atomic_write_json(path, updated)


def load_config() -> dict[str, Any]:
    global _CONFIG
    initialize_config()
    path = config_file()
    with file_lock(path):
        raw = path.read_bytes()
        value = json.loads(raw)
        version = (path, raw)
    _CONFIG = (version, value)
    return deepcopy(value)


def reload_config() -> dict[str, Any]:
    global _CONFIG, _DOTENV_CACHE
    _CONFIG = None
    _DOTENV_CACHE = None
    return load_config()


def settings() -> dict[str, Any]:
    path = config_file()
    with file_lock(path):
        version = (path, path.read_bytes()) if path.exists() else None
    cached = _CONFIG
    if cached is None or version != cached[0]:
        return load_config()
    return deepcopy(cached[1])


def _dotenv_values() -> dict[str, str]:
    """Read only the selected credential file, never an arbitrary project's .env."""
    global _DOTENV_CACHE
    files = [dotenv_file()]
    key = tuple(
        (str(path), path.stat().st_mtime_ns if path.is_file() else None)
        for path in files
    )
    if _DOTENV_CACHE is None:
        _DOTENV_CACHE = {}
    if key not in _DOTENV_CACHE:
        values = {}
        for path in files:
            if path.is_file():
                values.update(
                    {
                        str(k): str(v).strip()
                        for k, v in dotenv_values(path).items()
                        if v and str(v).strip()
                    }
                )
        _DOTENV_CACHE[key] = values
    return _DOTENV_CACHE[key]


def _config_scalar(key: str, cfg=None) -> str:
    raw = (settings() if cfg is None else cfg).get(key)
    if raw is not None and not isinstance(raw, (dict, list)):
        return raw.strip() if isinstance(raw, str) else str(raw).strip()
    return ""


def get_env(key: str, *, warn: bool = True, default: str = "", cfg=None) -> str:
    """配置读取唯一入口；/api 管理的 key 让 config.json 优先于 .env。"""
    if env_val := (os.environ.get(key) or "").strip():
        return env_val
    configured, dotenv = _config_scalar(key, cfg), _dotenv_values().get(key, "")
    value = (
        (configured or dotenv) if key in _API_CONFIG_KEYS else (dotenv or configured)
    )
    if not value and warn and not default:
        logger.warning("未配置 %r，请在 .env 或 config.json 根中填写。", key)
    return value or default


def _missing_keys(keys: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(key for key in keys if not get_env(key, warn=False).strip())


def missing_main_api_keys() -> tuple[str, ...]:
    _, params = get_model_and_params("coordinator")
    if params.get("gateway"):
        from redlotus.ModelGateway.model_factory import ModelTarget

        target = ModelTarget.for_role("coordinator")
        return () if target.api_key else (f"gateways.{params['gateway']}.api_key",)
    return _missing_keys(("BASE_URL", "API_KEY"))


def missing_rag_api_keys() -> tuple[str, ...]:
    return _missing_keys(("SILICONFLOW_BASE", "SILICONFLOW_KEY"))


def save_config(cfg: dict[str, Any] | None = None) -> None:
    cfg = settings() if cfg is None else cfg
    save_locked_json(config_file(), cfg)
    reload_config()


def update_config(change) -> None:
    """Apply a local edit to the latest document under the cross-process lock."""
    path = config_file()
    initialize_config()
    with file_lock(path):
        config = json.loads(path.read_text(encoding="utf-8"))
        change(config)
        atomic_write_json(path, config)
    reload_config()


def get_agent_usage_limits() -> "_UsageLimits":
    """单次 Agent 运行对模型请求次数上限"""
    cfg = settings()
    raw = cfg["request_limit"]
    if raw is None or str(raw).strip().lower() in ("none", "unlimited", "null", ""):
        return UsageLimits(request_limit=None)
    return UsageLimits(request_limit=int(raw))


def get_agent_run_policy() -> AgentRunPolicy:
    return AgentRunPolicy.from_config(settings())


def supported_thinking_efforts(model_name: str | None) -> tuple[str, ...]:
    from redlotus.ModelGateway.ModelChecker import _lookup_openrouter_meta

    meta = _lookup_openrouter_meta(model_name) if model_name else None
    configured = settings()["model_metadata"]["supported_thinking_efforts"]
    available = (meta or {}).get("supported_efforts") or configured
    return tuple(value for value in configured if value in available)


def role_supported_thinking_efforts(role: str) -> tuple[str, ...]:
    return supported_thinking_efforts(get_model_and_params(role)[0])


def apply_thinking_config(model_params, *, model_name=None):
    """Translate config thinking fields to Pydantic AI's common model settings."""
    params = deepcopy(model_params)
    thinking = str(params.pop("thinking", "")).strip().lower()
    effort = str(params.pop("reasoning_effort", "")).strip().lower()
    if thinking in ("disabled", "off", "false"):
        params["thinking"] = False
        if model_name and "deepseek" in model_name.lower():
            params["extra_body"] = {
                **params.get("extra_body", {}),
                "thinking": {"type": "disabled"},
            }
    elif thinking == "enabled":
        params["thinking"] = "xhigh" if effort == "max" else effort or True
    return params


def get_model_and_params(role: str, *, cfg=None) -> tuple[str, dict[str, Any]]:
    cfg = settings() if cfg is None else cfg
    raw = deepcopy(cfg["models"][role])
    if isinstance(raw, str):
        raw = {"preset": raw}
    if preset := raw.pop("preset", None):
        base = deepcopy(cfg["model_presets"][preset])
        raw = {**base.pop("settings", {}), **base, **raw.pop("settings", {}), **raw}
    name = str(raw.pop("name")).strip()
    raw = {**raw.pop("settings", {}), **raw}
    return name, raw


def set_model_name(role: str, model_name: str) -> None:
    selection = model_name.strip()

    def change(cfg):
        if selection in cfg.get("model_presets", {}):
            cfg["models"][role] = {"preset": selection}
        else:
            _, parameters = get_model_and_params(role, cfg=cfg)
            cfg["models"][role] = {"name": selection, **parameters}

    update_config(change)


def set_api(
    base_url: str | None = None,
    api_key: str | None = None,
    *,
    embedding_url: str | None = None,
    embedding_key: str | None = None,
) -> None:
    values = {
        "BASE_URL": base_url,
        "API_KEY": api_key,
        "SILICONFLOW_BASE": embedding_url,
        "SILICONFLOW_KEY": embedding_key,
    }
    if all(v is None for v in values.values()):
        return
    update_config(
        lambda cfg: cfg.update(
            {k: v.strip() for k, v in values.items() if v is not None}
        )
    )


def get_agent_roles(*, cfg=None) -> tuple[str, ...]:
    return tuple((settings() if cfg is None else cfg)["models"])


def get_context_profile_roles() -> tuple[str, ...]:
    roles = get_agent_roles()
    return tuple(role for role in roles if role in settings()["context"]) or roles


def get_context_config(role: str, *, cfg=None) -> dict[str, Any]:
    cfg = settings() if cfg is None else cfg
    raw = cfg.get("context", {})
    roles = get_agent_roles(cfg=cfg)
    if role not in roles:
        raise ValueError(f"Unknown Agent role: {role}")
    if not any(key in raw for key in roles):
        return dict(raw)  # Legacy shared context configuration.
    return {**raw.get("defaults", {}), **raw.get(role, {})}
