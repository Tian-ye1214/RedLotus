"""Configuration discovery, typed values and explicit model/role selection."""
from __future__ import annotations

import json
import os
import sys
from contextlib import ExitStack, contextmanager
from copy import deepcopy
from dataclasses import dataclass
from functools import lru_cache
from io import StringIO
from pathlib import Path
from typing import TYPE_CHECKING, Any

from dotenv.parser import parse_stream

if TYPE_CHECKING:
    from pydantic_ai.usage import UsageLimits as _UsageLimits

_CONFIG: tuple[tuple, dict[str, Any]] | None = None

@dataclass(frozen=True)
class AgentRunPolicy:
    max_concurrent_threads_per_session: int
    max_command_timeout_seconds: int

    @classmethod
    def from_config(cls, cfg: dict[str, Any]) -> "AgentRunPolicy":
        return cls(**{key: cfg["agent_run_policy"][key] for key in cls.__dataclass_fields__})

    def clamp_command_timeout(self, timeout: int) -> int:
        return max(1, min(int(timeout), self.max_command_timeout_seconds))

def _frozen() -> bool:
    return bool(getattr(sys, "frozen", False))

def user_config_dir() -> Path:
    """用户配置根：config.json / .env / bot config.yaml。"""
    return Path(
        os.environ.get("REDLOTUS_CONFIG_DIR")
        or Path.home() / ".redlotus"
    ).expanduser().resolve()

def local_config_root() -> Path:
    """开发覆盖只搜索一个目录；冻结程序使用 EXE 目录。"""
    return Path(sys.executable).resolve().parent if _frozen() else Path.cwd()

def config_sources() -> tuple[Path, Path, Path]:
    """配置优先级：本地 JSON、本地 .env、全局 JSON。"""
    local = os.environ.get("REDLOTUS_CONFIG_FILE")
    return (
        Path(local).expanduser().resolve() if local else local_config_root() / "src/redlotus/config.json",
        dotenv_file(),
        user_config_dir() / "config.json",
    )

def config_source_summary() -> str:
    """缺项提示使用可直接定位的三层文件路径。"""
    return " → ".join(map(str, config_sources()))

def config_file() -> Path:
    """配置命令只修改已有本地 JSON，否则修改全局 JSON。"""
    local, _, global_file = config_sources()
    return local if local.is_file() else global_file

def dotenv_file() -> Path:
    if path := os.environ.get("REDLOTUS_DOTENV_FILE"):
        return Path(path).expanduser().resolve()
    return local_config_root() / ".env"

class ConfigError(ValueError):
    """配置错误只包含字段和来源，不包含可能敏感的值。"""

    def __init__(self, message, *, path=(), missing=False):
        super().__init__(message)
        self.path, self.missing = path, missing

class ConfigValues(dict):
    """保持字典接口，同时为必填项和凭据保留路径及来源。"""

    def __init__(self, values=(), *, path=(), origins=None):
        self.path, self.origins = path, origins if origins is not None else {}
        super().__init__()
        for key, value in dict(values).items():
            self[key] = (
                ConfigValues(value, path=(*path, key), origins=self.origins)
                if isinstance(value, dict) else value
            )

    def __getitem__(self, key):
        if key not in self:
            raise ConfigError(f"缺少配置 {'.'.join((*self.path, key))}；检查来源: {config_source_summary()}")
        return super().__getitem__(key)

def _connection_field(key: str) -> bool:
    lowered = key.lower()
    return lowered in {"api_key", "base_url", "siliconflow_base", "siliconflow_key"} or lowered.endswith(("_api_key", "_base_url"))

def _model_selection_field(path: tuple[str, ...]) -> bool:
    return bool(path) and (
        path[0] == "RAG_models" or
        path[0] in {"models", "model_presets"} and path[-1] == "name"
    )


@lru_cache(maxsize=1)
def config_schema():
    """Public field contract shared by validation and setup; contains no configuration values."""
    from redlotus.runtime.resources import resource_root

    return json.loads((resource_root() / "config.schema.json").read_text(encoding="utf-8"))


def _resolve_schema(node):
    while "$ref" in node:
        node = {**config_schema()["$defs"][node["$ref"].rsplit("/", 1)[-1]],
                **{key: value for key, value in node.items() if key != "$ref"}}
    return node


def config_field(path):
    node = config_schema()
    for key in path:
        node = _resolve_schema(node)
        node = node.get("properties", {}).get(key, node.get("additionalProperties", {}))
    return _resolve_schema(node)


def config_value(values, path, default=None):
    for key in path:
        if not isinstance(values, dict) or key not in values:
            return default
        values = values[key]
    return values


def missing_startup_fields(values, required=()):
    """Resolve presets before finding required fields; new configured roles need no Python list."""
    schema = config_schema()
    roles = dict.fromkeys([*schema["properties"]["models"]["required"], *values.get("models", {})])
    if memory_role := config_value(values, ("memory_perception", "model_role")):
        roles[memory_role] = None
    effective, missing = deepcopy(values), []
    effective["models"] = {}
    for role in roles:
        try:
            name, params = get_model_and_params(role, cfg=values)
        except ConfigError as exc:
            if not exc.missing:
                raise
            missing.append(exc.path)
        else:
            effective["models"][role] = {"name": name, **params}
            if "auto_compress_ratio" in params:
                missing.extend(("models", role, key) for key in ("compress_head_turns", "compress_tail_turns") if key not in params)
    if missing:
        return list(dict.fromkeys(missing))

    def collect(node, current, path=()):
        node = _resolve_schema(node)
        for key in node.get("required", []):
            child = _resolve_schema(node["properties"][key])
            value = current.get(key) if isinstance(current, dict) else None
            field_path = (*path, key)
            if child.get("required"):
                yield from collect(child, value, field_path)
            elif key not in (current or {}) or value == "" and not child.get("allow_empty"):
                yield field_path

    return list(collect({**schema, "required": [*schema["required"], *required]}, effective))

def _validate_config(value, source: Path, path=()) -> None:
    """Validate supplied fields at each layer; setup checks the merged required fields."""
    field = config_field(path)
    kinds = field.get("type", [])
    kinds = [kinds] if isinstance(kinds, str) else kinds
    if not kinds and path and (_connection_field(path[-1]) or path[-1] == "api_key_env"):
        kinds = ["string", "null"]
    types = {"object": dict, "array": list, "string": str, "integer": int,
             "number": (int, float), "boolean": bool, "null": type(None)}
    valid = not kinds or any(isinstance(value, types[kind]) and (not isinstance(value, bool) or kind == "boolean") for kind in kinds)
    if isinstance(value, str) and "string_values" in field:
        valid = value.strip().lower() in field["string_values"]
        if not valid and "integer" in kinds:
            try:
                value = int(value)
                valid = True
            except ValueError:
                pass
    if valid and isinstance(value, (int, float)) and not isinstance(value, bool):
        valid = all(test for key, test in (
            ("minimum", value >= field.get("minimum", value)),
            ("exclusiveMinimum", value > field.get("exclusiveMinimum", value - 1)),
            ("maximum", value <= field.get("maximum", value)),
        ) if key in field)
    if not valid:
        raise ConfigError(f"配置 {source}: 字段 {'.'.join(path) or '<root>'} 类型或范围错误；{field.get('description', '')}", path=path)
    if isinstance(value, dict):
        for key, child in value.items():
            _validate_config(child, source, (*path, key))
    if isinstance(value, list) and field.get("items", {}).get("type") == "string" and not all(isinstance(item, str) for item in value):
        raise ConfigError(f"配置 {source}: 字段 {'.'.join(path)} 必须是字符串列表", path=path)


def _parse_config(path: Path, raw: bytes | None, *, dotenv=False) -> dict:
    """解析一个来源；.env 不做环境变量展开，嵌套键用双下划线。"""
    if raw is None:
        return {}
    try:
        if not dotenv:
            result = json.loads(raw)
        else:
            result = {}
            for binding in parse_stream(StringIO(raw.decode("utf-8-sig"))):
                if binding.error:
                    raise ConfigError(f"配置 {path}: 第 {binding.original.line} 行语法错误")
                if binding.key is None or binding.value is None:
                    continue
                try:
                    value = json.loads(binding.value)
                except json.JSONDecodeError:
                    value = binding.value
                parts = binding.key.split("__")
                node = result
                for key in parts[:-1]:
                    node = node.setdefault(key, {})
                    if not isinstance(node, dict):
                        raise ConfigError(f"配置 {path}: 字段 {binding.key} 结构冲突")
                if parts[-1] in node or not all(parts):
                    raise ConfigError(f"配置 {path}: 字段 {binding.key} 重复或结构冲突")
                node[parts[-1]] = value
        _validate_config(result, path)
        return result
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ConfigError(f"配置 {path}: 无效编码或 JSON，无法读取") from exc

def _merge_config(target, incoming, origins, source, path=()):
    """较高层逐字段覆盖；空白连接配置才继续使用低层值。"""
    for key, value in incoming.items():
        field = (*path, key)
        if _connection_field(key) and (value is None or isinstance(value, str) and not value.strip()):
            continue
        if _model_selection_field(field) and key in target and (
            value is None or isinstance(value, str) and not value.strip()
        ):
            continue
        if isinstance(value, dict):
            if not isinstance(target.get(key), dict):
                target[key] = {}
            _merge_config(target[key], value, origins, source, field)
        else:
            target[key] = deepcopy(value)
            origins[field] = source

@contextmanager
def _config_locks():
    """Acquire all configuration writer locks in a stable order."""
    from redlotus.runtime.resources import file_lock

    paths = set(path for path in config_sources() if path.exists())
    paths.add(config_file())
    with ExitStack() as locks:
        for path in sorted(paths):
            locks.enter_context(file_lock(path))
        yield

def settings() -> dict[str, Any]:
    """从各配置来源的一次完整字节快照解析，并返回独立副本。"""
    global _CONFIG
    version = tuple((path, path.read_bytes() if path.is_file() else None) for path in config_sources())
    cached = _CONFIG
    if cached is None or version != cached[0]:
        value, origins = {}, {}
        for index in reversed(range(len(version))):
            path, raw = version[index]
            _merge_config(value, _parse_config(path, raw, dotenv=index == 1), origins, index)
        cached = version, ConfigValues(value, origins=origins)
        _CONFIG = cached
    return deepcopy(cached[1])

def load_config() -> dict[str, Any]:
    global _CONFIG
    _CONFIG = None
    return settings()

reload_config = load_config

def credential_value(gateway_name: str, cfg: dict) -> str:
    """命名网关的直接值和命名引用按各自来源排序，同层优先直接值。"""
    gateway = cfg["gateways"][gateway_name]
    candidates = [(gateway.get("api_key"), ("gateways", gateway_name, "api_key"))]
    if reference := gateway.get("api_key_env"):
        candidates.append((get_env(reference, warn=False, cfg=cfg), (reference,)))
    origins = getattr(cfg, "origins", {})
    candidates.sort(key=lambda item: origins.get(item[1], 0))
    return next((str(value).strip() for value, _ in candidates if value is not None and str(value).strip()), "")

def get_env(key: str, *, warn: bool = True, default: str = "", cfg=None) -> str:
    """旧标量读取接口共享配置快照；不从宿主环境变量读取业务值。"""
    configuration = settings() if cfg is None else cfg
    raw = configuration.get(key)
    if isinstance(raw, (dict, list)):
        raise ConfigError(f"配置字段 {key} 必须是标量；检查来源: {config_source_summary()}")
    value = str(raw).strip() if raw is not None else ""
    if not value and warn and not default:
        raise ConfigError(f"缺少配置 {key}；检查来源: {config_source_summary()}")
    return value or default

def _missing_keys(keys: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(key for key in keys if not get_env(key, warn=False).strip())

def missing_main_api_keys() -> tuple[str, ...]:
    _, params = get_model_and_params("coordinator")
    if params.get("gateway"):
        from redlotus.runtime.network import ModelTarget

        target = ModelTarget.for_role("coordinator")
        return () if target.api_key else (f"gateways.{params['gateway']}.api_key",)
    return _missing_keys(("BASE_URL", "API_KEY"))

def missing_rag_api_keys() -> tuple[str, ...]:
    return _missing_keys(("SILICONFLOW_BASE", "SILICONFLOW_KEY"))

def _persist_changes(target, before, after):
    """只把用户改动落到写入层，不复制回退层的其他字段。"""
    for key in before.keys() - after.keys():
        target.pop(key, None)
    for key, value in after.items():
        if key in before and before[key] == value:
            continue
        if isinstance(value, dict) and isinstance(before.get(key), dict):
            child = target.setdefault(key, {})
            _persist_changes(child, before[key], value)
        else:
            target[key] = deepcopy(value)

def update_config(change) -> None:
    """锁内读取最新有效值，只保存本次编辑产生的差异。"""
    from redlotus.runtime.resources import atomic_write_json

    path = config_file()
    try:
        with _config_locks():
            path = config_file()
            raw = _parse_config(path, path.read_bytes() if path.is_file() else None)
            before = settings()
            after = deepcopy(before)
            change(after)
            _persist_changes(raw, before, after)
            _validate_config(raw, path)
            atomic_write_json(path, raw)
    except OSError as exc:
        raise ConfigError(f"无法写入配置 {path}: {exc}") from None
    reload_config()

def get_agent_usage_limits() -> "_UsageLimits":
    """单次 Agent 运行对模型请求次数上限"""
    from pydantic_ai.usage import UsageLimits

    cfg = settings()
    raw = cfg["request_limit"]
    if raw is None or str(raw).strip().lower() in ("none", "unlimited", "null", ""):
        return UsageLimits(request_limit=None)
    return UsageLimits(request_limit=int(raw))

def get_agent_run_policy() -> AgentRunPolicy:
    """Construct the execution policy only when configuration is requested."""

    return AgentRunPolicy.from_config(settings())

def supported_thinking_efforts(model_name: str | None) -> tuple[str, ...]:
    from redlotus.runtime.network import _lookup_openrouter_meta

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
    raw = deepcopy((cfg.get("models") or {}).get(role) or {})
    if isinstance(raw, str):
        raw = {"preset": raw}
    preset = raw.pop("preset", None)
    if preset is not None and str(preset).strip():
        if not isinstance(preset, str) or preset not in cfg.get("model_presets", {}):
            raise ConfigError(
                f"配置 models.{role}.preset 引用了不存在的 model_presets.{preset}；"
                f"检查来源: {config_source_summary()}"
            )
        base = deepcopy(cfg["model_presets"][preset])
        origins = getattr(cfg, "origins", {})
        selection = origins.get(("models", role, "preset"), 0)
        raw = {key: value for key, value in raw.items()
               if min((rank for path, rank in origins.items()
                       if path[:3] == ("models", role, key)), default=0) <= selection}
        raw = {**base.pop("settings", {}), **base, **raw.pop("settings", {}), **raw}
    raw = {**raw.pop("settings", {}), **raw}
    gateway = raw.get("gateway")
    if gateway is not None and str(gateway).strip():
        if not isinstance(gateway, str) or gateway not in cfg.get("gateways", {}):
            raise ConfigError(
                f"配置 models.{role}.gateway 引用了不存在的 gateways.{gateway}；"
                f"检查来源: {config_source_summary()}"
            )
        raw["gateway"] = gateway
    name = raw.pop("name", None)
    if not isinstance(name, str) or not name.strip():
        path = ("model_presets", preset, "name") if preset in cfg.get("model_presets", {}) else ("models", role, "name")
        raise ConfigError(f"缺少有效配置 {'.'.join(path)}；检查来源: {config_source_summary()}", path=path, missing=True)
    name = name.strip()
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

def get_agent_roles(*, cfg=None) -> tuple[str, ...]:
    return tuple((settings() if cfg is None else cfg)["models"])

def get_context_profile_roles() -> tuple[str, ...]:
    cfg = settings()
    return tuple(
        role for role in get_agent_roles(cfg=cfg)
        if "auto_compress_ratio" in get_context_config(role, cfg=cfg)
    )

def get_context_config(role: str, *, cfg=None) -> dict[str, Any]:
    _, parameters = get_model_and_params(role, cfg=cfg)
    fields = ("max_context_windows", "auto_compress_ratio", "compress_head_turns", "compress_tail_turns")
    return {key: parameters[key] for key in fields if key in parameters}
