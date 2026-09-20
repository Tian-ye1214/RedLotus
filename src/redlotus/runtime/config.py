"""Runtime config responsibilities."""

from __future__ import annotations

import json
from contextlib import ExitStack, contextmanager
from copy import deepcopy
from io import StringIO
from pathlib import Path
from typing import Any

from dotenv.parser import parse_stream

from redlotus.runtime.files import (
    atomic_write_json,
    config_file,
    config_source_summary,
    config_sources,
    file_lock,
)

_CONFIG: tuple[tuple, dict[str, Any]] | None = None
_POLICY_TYPES = {
    "request_limit": (int, str, type(None)),
    "lifecycle.shutdown_grace_seconds": (int, float),
    "lifecycle.invocation_history_per_session": int,
    "agent_run_policy.max_concurrent_threads_per_session": int,
    "agent_run_policy.max_command_timeout_seconds": int,
    "storage.state_dir": str,
    "storage.project_dir": str,
    "storage.sessions_dir": str,
    "storage.references_dir": str,
    "storage.runtime_dir": str,
    "storage.project_logs_dir": str,
    "memory_perception.model_role": str,
    "memory_perception.window_turns": int,
    "memory_perception.overlap_turns": int,
    "short_term_memory.db_path": str,
    "short_term_memory.table_name": str,
    "short_term_memory.vector_search_limit": int,
    "short_term_memory.final_top_k": int,
    "short_term_memory.use_rerank": bool,
    "short_term_memory.min_similarity": (int, float),
    "short_term_memory.turn_token_limit": int,
    "short_term_memory.turn_chunk_overlap_tokens": int,
    "short_term_memory.index": dict,
    "short_term_memory.index.min_rows": int,
    "short_term_memory.index.metric": str,
    "short_term_memory.index.rebuild_every_n_adds": int,
    "short_term_memory.index.rows_per_partition": int,
    "short_term_memory.index.dimensions_per_sub_vector": int,
    "long_term_memory.table_name": str,
    "rag_service.http2": bool,
    "rag_service.timeout": (int, float),
    "rag_service.embedding_batch_size": int,
    "rag_service.index_batch_size": int,
    "model_metadata.url": str,
    "model_metadata.timeout": (int, float),
    "model_metadata.supported_thinking_efforts": list,
}


class ConfigError(ValueError):
    """配置错误只包含字段和来源，不包含可能敏感的值。"""


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


def _selected_model_name_path(
    role: str,
    cfg: dict[str, Any],
    schema: dict[str, Any] | None = None,
) -> tuple[str, ...]:
    """Locate the field that owns a selected role's model name."""
    selected = (cfg.get("models") or {}).get(role)
    if selected is None and schema is not None:
        selected = (schema.get("models") or {}).get(role)
    preset = selected if isinstance(selected, str) else (selected or {}).get("preset")
    presets = (cfg.get("model_presets") or {}).copy()
    if schema is not None:
        presets = {**(schema.get("model_presets") or {}), **presets}
    if isinstance(preset, str) and preset.strip() and preset in presets:
        return "model_presets", preset, "name"
    return "models", role, "name"


def _model_roles(schema: dict[str, Any], values: dict[str, Any]) -> list[str]:
    """List roles declared by the shipped schema and any configured extensions."""
    return list(
        dict.fromkeys(
            [*(schema.get("models") or {}), *(values.get("models") or {})]
        )
    )


def _validate_config(value, source: Path, path=()) -> None:
    """校验结构和服务字段；不提供任何模型或策略默认值。"""
    objects = {"models", "gateways", "model_presets", "RAG_models", "context", "storage",
               "bot", "lifecycle", "memory_perception", "agent_run_policy",
               "short_term_memory", "long_term_memory", "model_metadata",
               "rag_service", "input_limits", "task_title", "conversation_log"}
    key = path[-1] if path else ""
    field = ".".join(path)
    expected = _POLICY_TYPES.get(field)
    if not path or len(path) == 1 and key in objects:
        expected = dict
    elif len(path) == 2 and path[0] in {"gateways", "model_presets"}:
        expected = dict
    elif len(path) == 2 and path[0] == "models":
        expected = (dict, str)
    elif key == "input_limits" or len(path) == 2 and path[0] == "input_limits" or path == ("storage", "cleanup"):
        expected = dict
    elif path and path[0] in {"models", "model_presets"} and key == "settings":
        expected = dict
    elif _connection_field(key) or key == "api_key_env" or (
        len(path) == 2 and path[0] == "RAG_models"
        or path and path[0] == "storage" and key.endswith("_dir")
    ):
        expected = str
    elif path and path[0] == "gateways" and key in {"timeout", "connect_timeout"}:
        expected = (int, float)
    elif path and path[0] in {"models", "model_presets"} and key in {"max_tokens", "temperature", "top_p"}:
        expected = int if key == "max_tokens" else (int, float)
    elif path == ("MODEL_HTTP_TIMEOUT",):
        expected = (int, float)
    elif "input_limits" in path and key in {"max_files", "max_file_bytes", "max_request_bytes"}:
        expected = (int, type(None)) if key == "max_request_bytes" else int
    elif field in {"storage.cleanup.enabled", "storage.cleanup.execution_cache"}:
        expected = bool
    elif field == "storage.cleanup.session_retention_days":
        expected = (int, float)
    if expected and (value is not None or field in _POLICY_TYPES or expected in {dict, bool} or "input_limits" in path or key in {"timeout", "connect_timeout", "MODEL_HTTP_TIMEOUT", "session_retention_days"}) and (not isinstance(value, expected) or isinstance(value, bool) and expected != bool):
        types = expected if isinstance(expected, tuple) else (expected,)
        raise ConfigError(f"配置 {source}: 字段 {field or '<root>'} 类型错误，应为 {' / '.join(kind.__name__ for kind in types)}；参考 docs/design.md#configuration-reference")
    if (field in _POLICY_TYPES or "input_limits" in path or key in {"timeout", "connect_timeout", "MODEL_HTTP_TIMEOUT", "session_retention_days"}) and isinstance(value, (int, float)) and not isinstance(value, bool):
        import math

        zero_allowed = key in {"overlap_turns", "turn_chunk_overlap_tokens", "max_files"}
        if not math.isfinite(value) or key != "min_similarity" and (value < 0 if zero_allowed else value <= 0):
            raise ConfigError(f"配置 {source}: 字段 {field} 数值范围错误")
    if field == "request_limit" and isinstance(value, str) and value.strip().lower() not in {"", "none", "null", "unlimited"}:
        if not value.strip().isdecimal() or int(value) <= 0:
            raise ConfigError(f"配置 {source}: 字段 request_limit 需要正整数或 null/unlimited")
    if expected is list and isinstance(value, list):
        for index, item in enumerate(value):
            if not isinstance(item, str):
                raise ConfigError(f"配置 {source}: 字段 {'.'.join(path)}.{index} 类型错误")
    if isinstance(value, dict):
        for name, child in value.items():
            _validate_config(child, source, (*path, name))


def validate_runtime_configuration(cfg=None) -> None:
    """Report missing runtime fields together before constructing any Agent."""
    cfg = settings() if cfg is None else cfg
    _validate_config(cfg, config_source_summary())
    missing = []
    rag_enabled = bool(cfg.get("RAG_models", {}).get("embedding") and cfg.get("SILICONFLOW_BASE") and cfg.get("SILICONFLOW_KEY"))
    for field in _POLICY_TYPES:
        if (field.startswith(("rag_service.", "short_term_memory.index.")) or field in {"short_term_memory.vector_search_limit", "short_term_memory.min_similarity"}) and not rag_enabled:
            continue
        if field.startswith("model_metadata.") and "model_metadata" not in cfg:
            continue
        node = cfg
        for key in field.split("."):
            if not isinstance(node, dict) or key not in node:
                missing.append(field)
                break
            node = node[key]
        else:
            if isinstance(node, str) and not node.strip() and field not in {"storage.state_dir", "request_limit"}:
                missing.append(field)
    if not cfg.get("models"):
        missing.append("models：声明内置功能使用的角色；见字段参考")
    for role in get_agent_roles(cfg=cfg) if cfg.get("models") else ():
        try:
            _, params = get_model_and_params(role, cfg=cfg)
            context = get_context_config(role, cfg=cfg)
        except ConfigError as exc:
            missing.append(str(exc))
            continue
        gateway = cfg.get("gateways", {}).get(params.get("gateway"), {})
        if "timeout" not in gateway and "MODEL_HTTP_TIMEOUT" not in cfg:
            missing.append("MODEL_HTTP_TIMEOUT")
        limits = {**cfg.get("input_limits", {}).get("defaults", {}), **gateway.get("input_limits", {}),
                  **params.get("input_limits", {}), **cfg.get("input_limits", {}).get(role, {})}
        for field in ("max_files", "max_file_bytes"):
            if field not in limits:
                missing.append(f"input_limits.{role}.{field} (或 defaults / gateway / model)")
        if "auto_compress_ratio" in context:
            missing.extend(f"models.{role}.{field}" for field in ("compress_head_turns", "compress_tail_turns") if field not in context)
            if context.get("max_context_windows") is None and "model_metadata" not in cfg:
                missing.extend(field for field in _POLICY_TYPES if field.startswith("model_metadata."))
    perception = cfg.get("memory_perception", {})
    if perception.get("model_role") and perception["model_role"] not in cfg.get("models", {}):
        missing.append("memory_perception.model_role 必须引用 models 中声明的角色")
    for group, lower, upper in (("memory_perception", "overlap_turns", "window_turns"),
                                ("short_term_memory", "turn_chunk_overlap_tokens", "turn_token_limit")):
        values = cfg.get(group, {})
        if lower in values and upper in values and values[lower] >= values[upper]:
            missing.append(f"{group}.{lower} 必须小于 {group}.{upper}")
    if missing:
        raise ConfigError("运行配置不完整或无效：\n- " + "\n- ".join(dict.fromkeys(missing))
                          + f"\n请编辑 {config_file()}；字段类型、单位及角色声明见 docs/design.md#configuration-reference。"
                          + f"\n检查来源: {config_source_summary()}")


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
        if not isinstance(result, dict):
            raise ConfigError(f"配置 {path}: 根节点必须是对象")
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
    paths = set(path for path in config_sources() if path.exists())
    paths.add(config_file())
    with ExitStack() as locks:
        for path in sorted(paths):
            locks.enter_context(file_lock(path))
        yield


def _config_version():
    return tuple((path, path.read_bytes() if path.is_file() else None) for path in config_sources())


def settings() -> dict[str, Any]:
    """从各配置来源的一次完整字节快照解析，并返回独立副本。"""
    global _CONFIG
    version = _config_version()
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
    from redlotus.runtime.resources import _workspace_log_dirs
    global _CONFIG
    _CONFIG = None
    _workspace_log_dirs.clear()
    return settings()


def reload_config() -> dict[str, Any]:
    return load_config()


def initialize_user_configuration() -> Path:
    """Validate user-selected sources without copying or creating configuration files."""
    load_config()
    return config_file()


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
        from redlotus.models.providers import ModelTarget

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


def get_agent_usage_limits():
    """单次 Agent 运行对模型请求次数上限"""
    from pydantic_ai.usage import UsageLimits

    cfg = settings()
    raw = cfg["request_limit"]
    if raw is None or str(raw).strip().lower() in ("none", "unlimited", "null", ""):
        return UsageLimits(request_limit=None)
    return UsageLimits(request_limit=int(raw))


def get_agent_run_policy():
    """Construct the execution policy only when configuration is requested."""
    from redlotus.runtime.context import AgentRunPolicy

    return AgentRunPolicy.from_config(settings())


def supported_thinking_efforts(model_name: str | None) -> tuple[str, ...]:
    from redlotus.models.context import _lookup_openrouter_meta

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
        field = ".".join(_selected_model_name_path(role, cfg))
        raise ConfigError(f"缺少有效配置 {field}；检查来源: {config_source_summary()}")
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
    cfg = settings() if cfg is None else cfg
    _, parameters = get_model_and_params(role, cfg=cfg)
    fields = ("max_context_windows", "auto_compress_ratio", "compress_head_turns", "compress_tail_turns")
    result = {key: deepcopy(parameters[key]) for key in fields if key in parameters}
    for key, value in result.items():
        if key == "max_context_windows" and value is None:
            continue
        valid = (
            isinstance(value, (int, float)) and not isinstance(value, bool) and 0 < value <= 1
            if key == "auto_compress_ratio"
            else isinstance(value, int) and not isinstance(value, bool)
            and (value > 0 if key == "max_context_windows" else value >= 0)
        )
        if not valid:
            raise ConfigError(f"无效配置 models.{role}.{key}；检查来源: {config_source_summary()}")
    return result
