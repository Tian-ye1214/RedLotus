"""Configuration discovery, typed values and explicit model/role selection."""
from __future__ import annotations

import json
import os
import sys
from contextlib import ExitStack, contextmanager
from collections.abc import Mapping
from copy import deepcopy
from functools import partial
from io import StringIO
from pathlib import Path
from typing import TYPE_CHECKING, Any

from dotenv.parser import parse_stream


if TYPE_CHECKING:
    from pydantic_ai.usage import UsageLimits as _UsageLimits

def _frozen() -> bool:
    return bool(getattr(sys, "frozen", False))

def user_config_dir() -> Path:
    """用户配置根：config.json / bot config.yaml。"""
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

def _empty_value(value, kind):
    kinds = kind if isinstance(kind, tuple) else (kind,)
    return kind is not None and (
        value is None and type(None) not in kinds or
        isinstance(value, str) and not value.strip() and (str not in kinds or type(None) not in kinds))

class ConfigValues(dict):
    """按实际访问的字段查找三层来源；字典枚举才请求该节点的全部字段。"""

    def __init__(self, values=(), *, path=(), sources=()):
        self.path = path
        self.sources, self.deleted = sources, set()
        super().__init__(values)

    def _nodes(self):
        for rank, node in enumerate(self.sources):
            if callable(node):
                node = self.sources[rank] = node()
            for key in self.path:
                if not isinstance(node, dict):
                    break
                node = node.get(key)
            if isinstance(node, dict):
                yield rank, node
            elif node is not None and not (isinstance(node, str) and not node.strip()):
                raise ConfigError(f"配置 {'.'.join(self.path)} 必须是对象；检查来源: {config_source_summary()}", path=self.path)

    def __contains__(self, key):
        return self._resolve(key, None)

    def _resolve(self, key, kind):
        if dict.__contains__(self, key) and not _empty_value(dict.__getitem__(self, key), kind):
            return True
        if key in self.deleted:
            return False
        field = (*self.path, key)
        declared_role = False
        for _, node in self._nodes():
            if not (node._resolve(key, kind) if isinstance(node, ConfigValues) else key in node):
                continue
            value = node[key]
            if _empty_value(value, kind):
                continue
            if (_connection_field(key) or _model_selection_field(field)) and (
                value is None or isinstance(value, str) and not value.strip()
            ):
                declared_role |= field[0] == "models" and len(field) == 2
                continue
            self[key] = (ConfigValues(path=field, sources=self.sources)
                         if isinstance(value, dict) else deepcopy(value))
            return True
        if declared_role:
            self[key] = ConfigValues(path=field, sources=self.sources)
            return True
        return False

    def __getitem__(self, key):
        if key not in self:
            raise KeyError(".".join((*self.path, key)))
        return dict.__getitem__(self, key)

    def get(self, key, default=None):
        return self[key] if key in self else default

    def __iter__(self):
        for _, node in self._nodes():
            for key in node:
                self.__contains__(key)
        return dict.__iter__(self)

    def __len__(self):
        return sum(1 for _ in self)

    keys, items, values = Mapping.keys, Mapping.items, Mapping.values

    def setdefault(self, key, default=None):
        if key not in self:
            self[key] = default
        return self[key]

    def pop(self, key, *default):
        self.__contains__(key)
        self.deleted.add(key)
        return dict.pop(self, key, *default)

    def __delitem__(self, key):
        self.pop(key)

    def __deepcopy__(self, memo):
        values = {key: deepcopy(value, memo) for key, value in (self.items() if self.path else dict.items(self))}
        result = ConfigValues(values, path=self.path, sources=deepcopy(self.sources, memo))
        result.deleted = self.deleted.copy()
        return result

def _connection_field(key: str) -> bool:
    lowered = key.lower()
    return lowered in {"api_key", "base_url", "siliconflow_base", "siliconflow_key"} or lowered.endswith(("_api_key", "_base_url"))

def _model_selection_field(path: tuple[str, ...]) -> bool:
    return bool(path) and (
        path[0] == "RAG_models" or
        path[0] == "models" and len(path) == 2 or
        path[0] == "models" and path[-1] == "name"
    )


def config_value(values, path, default=None, *, purpose=None, kind=None, choices=None):
    full_path = (*getattr(values, "path", ()), *path)
    for index, key in enumerate(path):
        if not isinstance(values, dict) or not (
            values._resolve(key, kind if index == len(path) - 1 else dict)
            if isinstance(values, ConfigValues) else key in values and not _empty_value(
                values[key], kind if index == len(path) - 1 else dict)
        ):
            if purpose is None:
                return default
            if not (_connection_field(path[-1]) or _model_selection_field(full_path) or full_path == ("models",)):
                raise KeyError(".".join(full_path))
            kinds = kind if isinstance(kind, tuple) else (kind,)
            raise ConfigError(f"缺少配置 {'.'.join(full_path)}；作用：{purpose}；"
                              f"类型：{' / '.join(t.__name__ for t in kinds)}；"
                              f"可选值：{choices if choices is not None else '无枚举限制'}；"
                              f"检查来源: {config_source_summary()}", path=full_path, missing=True)
        values = values[key]
    if kind is not None and (not isinstance(values, kind) or isinstance(values, bool) and kind is not bool
                            and (not isinstance(kind, tuple) or bool not in kind)):
        raise ConfigError(f"配置 {'.'.join(full_path)} 类型错误；作用：{purpose}；检查来源: {config_source_summary()}", path=full_path)
    return values


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
        if not isinstance(result, dict):
            raise ConfigError(f"配置 {path}: JSON 根节点必须是对象")
        return result
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ConfigError(f"配置 {path}: 无效编码或 JSON，无法读取") from exc

def _read_config(path, *, dotenv):
    return _parse_config(path, path.read_bytes() if path.is_file() else None, dotenv=dotenv)

@contextmanager
def _config_locks(timeout):
    """Acquire all configuration writer locks in a stable order."""
    from redlotus.runtime.resources import file_lock

    paths = set(path for path in (config_sources()[0], config_sources()[-1]) if path.exists())
    paths.add(config_file())
    with ExitStack() as locks:
        for path in sorted(paths):
            locks.enter_context(file_lock(path, timeout=timeout))
        yield

def settings() -> dict[str, Any]:
    """逐参数先项目 JSON，再项目 .env，最后全局 JSON；不读取未用的低层。"""
    sources = [partial(_read_config, path, dotenv=index == 1)
               for index, path in enumerate(config_sources())]
    sources[0] = sources[0]()
    return ConfigValues(sources=sources)

load_config = reload_config = settings

def get_env(key: str, *, warn: bool = True, default: str = "", cfg=None) -> str:
    """旧标量读取接口共享配置快照；不从宿主环境变量读取业务值。"""
    purpose = "模型或检索服务地址" if "BASE" in key.upper() or "URL" in key.upper() else "服务认证或连接参数"
    raw = config_value(settings() if cfg is None else cfg, (key,), default,
                       purpose=purpose if warn and not default else None,
                       kind=str if _connection_field(key) else (str, int, float, bool))
    return (str(raw).strip() if raw is not None else "") or default

def missing_main_api_keys() -> tuple[str, ...]:
    return tuple(key for key in ("BASE_URL", "API_KEY") if not get_env(key, warn=False))

def missing_rag_api_keys() -> tuple[str, ...]:
    return tuple(key for key in ("SILICONFLOW_BASE", "SILICONFLOW_KEY") if not get_env(key, warn=False))

def _persist_changes(target, before, after):
    """只把用户改动落到写入层，不复制回退层的其他字段。"""
    for key in before.keys() - after.keys():
        target.pop(key, None)
    for key, value in after.items():
        if key in before and before[key] == value:
            continue
        if isinstance(value, dict) and isinstance(before.get(key), dict):
            nested = target.get(key)
            if not isinstance(nested, dict):
                nested = {}
            _persist_changes(nested, before[key], value)
            if nested:
                target[key] = nested
        else:
            target[key] = deepcopy(value)

def update_config(change, *, lock_timeout=None) -> None:
    """锁内读取最新有效值，只保存本次编辑产生的差异。"""
    from redlotus.runtime.resources import atomic_write_json

    path = config_file()
    try:
        with _config_locks(lock_timeout):
            path = config_file()
            raw = _parse_config(path, path.read_bytes() if path.is_file() else None)
            before = settings()
            after = deepcopy(before)
            change(after)
            _persist_changes(raw, before, after)
            atomic_write_json(path, raw)
    except OSError as exc:
        raise ConfigError(f"无法写入配置 {path}: {exc}") from None

def get_agent_usage_limits() -> "_UsageLimits":
    """单次 Agent 运行对模型请求次数上限"""
    from pydantic_ai.usage import UsageLimits

    cfg = settings()
    raw = config_value(cfg, ("request_limit",), purpose="单次 Agent 模型请求上限；null 或空字符串表示不限制", kind=(int, str, type(None)))
    if raw is None or str(raw).strip().lower() in ("none", "unlimited", "null", ""):
        return UsageLimits(request_limit=None)
    return UsageLimits(request_limit=int(raw))

def supported_thinking_efforts(model_name: str | None) -> tuple[str, ...]:
    from redlotus.runtime.network import _lookup_openrouter_meta

    meta = _lookup_openrouter_meta(model_name) if model_name else None
    configured = config_value(settings(), ("model_metadata", "supported_thinking_efforts"), purpose="可选择的思考档位名称", kind=list)
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
    models = cfg.get("models")
    if not isinstance(models, dict):
        raise ConfigError(f"配置 models 必须是对象；检查来源: {config_source_summary()}", path=("models",))
    raw = deepcopy(models.get(role, {}))
    if not isinstance(raw, dict):
        raise ConfigError(f"配置 models.{role} 必须是模型对象；检查来源: {config_source_summary()}", path=("models", role))
    name = raw.pop("name", None)
    if name is not None and not isinstance(name, str):
        raise ConfigError(f"配置 models.{role}.name 必须是字符串；检查来源: {config_source_summary()}")
    if not isinstance(name, str) or not name.strip():
        config_value({}, ("models", role, "name"), purpose="选择 Agent 模型，使用服务:模型标识", kind=str)
    name = name.strip()
    return name, raw

def set_model_name(role: str, model_name: str) -> None:
    selection = model_name.strip()

    def change(cfg):
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
    fields = ("max_context_windows", "max_tokens", "auto_compress_ratio", "compress_head_turns", "compress_tail_turns")
    return {key: parameters[key] for key in fields if key in parameters}
