# LLM: 运行配置按原来源优先级合并；宿主模型 profile_id 必须随来源保留，让孙代理恢复同一配置。
# 模块用途: 叠加 owner/task/run 的明确配置，不丢失未被覆盖字段的模型身份，也不公开任意元数据。
from __future__ import annotations

"""Runtime config override layers with source tracing.

The base config loader owns schema defaults, YAML and environment overrides.
This module only adds scoped runtime overlays such as owner, workspace, task,
run and agent-local overrides.  It deliberately reuses ConfigLayer /
EffectiveConfig so runtime code does not invent a second config hierarchy.
"""

from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .config_io import load_simple_yaml
from .config_layers import ConfigLayer, EffectiveConfig
from .config_sources import public_config_keys
from .normalize import normalize_agent_config

_SCOPE_PRIORITIES = {
    "global": 25,
    "shared": 25,
    "owner": 30,
    "workspace": 40,
    "task": 50,
    "run": 60,
    "agent": 70,
    "runtime": 90,
}
_LOADED_CONFIG_DEFAULT_SOURCE = {"source": "loaded_agent_config", "priority": 20}


# LLM: 作用域配置层；仅原优先级合并，保留管理员源与强制开关，联测owner覆盖隔离。
# 类用途: 保存一次作用域配置覆盖及来源，不作为权限凭证。
@dataclass(frozen=True)
class RuntimeConfigLayer:
    scope: str
    source: str
    values: Mapping[str, Any] = field(default_factory=dict)
    priority: int | None = None
    warnings: tuple[str, ...] = ()

    # LLM: 作用域配置层；仅原优先级合并，保留管理员源与强制开关，联测owner覆盖隔离。
    # 函数用途: 将运行时覆盖投影成通用配置层，保留作用域和来源。
    def to_config_layer(self) -> ConfigLayer:
        return ConfigLayer(
            source=self.source,
            priority=runtime_scope_priority(self.scope, self.priority),
            values=self.values,
        )


# LLM: 作用域配置层；仅原优先级合并，保留管理员源与强制开关，联测owner覆盖隔离。
# 类用途: 承载构造运行时配置层所需的作用域、路径和配置事实。
@dataclass(frozen=True)
class RuntimeConfigLayerRequest:
    scope: str
    source: str
    values: Mapping[str, Any]
    config_cls: type
    priority: int | None = None


# LLM: 作用域配置层；仅原优先级合并，保留管理员源与强制开关，联测owner覆盖隔离。
# 函数用途: 返回运行时作用域的固定优先级，不由请求正文决定。
def runtime_scope_priority(scope: str, explicit: int | None = None) -> int:
    if explicit is not None:
        return int(explicit)
    return _SCOPE_PRIORITIES.get(str(scope or "").strip().lower(), 70)


# LLM: 作用域配置层；仅原优先级合并，保留管理员源与强制开关，联测owner覆盖隔离。
# 函数用途: 校验并构造一层运行配置，拒绝内部诊断字段进入覆盖。
def make_runtime_config_layer(request: RuntimeConfigLayerRequest) -> RuntimeConfigLayer:
    """Normalize one scoped override layer and reject internal diagnostics fields."""

    allowed = public_config_keys(request.config_cls)
    raw_values = dict(request.values)
    normalized, normalize_warnings = normalize_agent_config(raw_values)
    unknown = sorted(key for key in raw_values if key not in allowed)
    warnings = list(normalize_warnings)
    warnings.extend(f"unknown runtime config key: {key!r}; ignored" for key in unknown)
    clean = {
        key: normalized[key]
        for key in raw_values
        if key in allowed and key in normalized and normalized[key] is not None
    }
    return RuntimeConfigLayer(
        scope=request.scope,
        source=request.source,
        values=clean,
        priority=request.priority,
        warnings=tuple(warnings),
    )


# LLM: 作用域配置层；仅原优先级合并，保留管理员源与强制开关，联测owner覆盖隔离。
# 函数用途: 从指定小型YAML文件读取运行配置层，保留原格式错误处理。
def load_runtime_config_layer(
    path: str | Path,
    *,
    scope: str,
    config_cls: type,
    priority: int | None = None,
) -> RuntimeConfigLayer:
    """Load a small YAML override file as one scoped runtime layer."""

    resolved = Path(path).expanduser().resolve()
    raw = load_simple_yaml(resolved) if resolved.exists() else {}
    return make_runtime_config_layer(
        RuntimeConfigLayerRequest(
            scope=scope,
            source=str(resolved),
            values=raw,
            config_cls=config_cls,
            priority=priority,
        )
    )


# LLM: 作用域配置层；仅原优先级合并，保留管理员源与强制开关，联测owner覆盖隔离。
# 函数用途: 把作用域覆盖合入已加载配置，保留管理员来源和强制锁。
def merge_runtime_config_layers(
    base_config: object,
    layers: list[RuntimeConfigLayer] | tuple[RuntimeConfigLayer, ...],
    *,
    config_cls: type | None = None,
) -> EffectiveConfig:
    """Merge runtime overlays onto an already-loaded config.

    Existing per-field sources on ``base_config`` are preserved.  A scoped layer
    only wins if its priority is at least the current winning source priority,
    so environment-provided secrets are not silently overwritten by a lower
    owner or task layer.
    """

    config_type = config_cls or type(base_config)
    allowed = public_config_keys(config_type)
    values = _base_config_values(base_config, allowed)
    sources = _base_config_sources(base_config, values)
    layer_records = list(_base_config_layers(base_config))
    for layer in sorted((item.to_config_layer() for item in layers), key=lambda item: item.priority):
        layer_records.append({"source": layer.source, "priority": layer.priority})
        _apply_runtime_config_layer(values=values, sources=sources, layer=layer, allowed=allowed)
    return EffectiveConfig(values=values, sources=sources, layers=tuple(layer_records))


# LLM: 作用域配置层；仅原优先级合并，保留管理员源与强制开关，联测owner覆盖隔离。
# 函数用途: 取得基础配置中的公开值，避免复制内部运行状态。
def _base_config_values(config: object, allowed: set[str]) -> dict[str, Any]:
    return {key: getattr(config, key) for key in allowed if hasattr(config, key)}


# LLM: 作用域配置层；仅原优先级合并，保留管理员源与强制开关，联测owner覆盖隔离。
# 函数用途: 复制基础配置的来源信息，供后续叠加保留管理员原值。
def _base_config_sources(config: object, values: Mapping[str, Any]) -> dict[str, dict[str, object]]:
    raw_sources = getattr(config, "config_sources", {}) if config is not None else {}
    sources: dict[str, dict[str, object]] = {}
    for key in values:
        source = raw_sources.get(key) if isinstance(raw_sources, dict) else None
        sources[key] = _source_record(source)
    return sources


# LLM: 作用域配置层；仅原优先级合并，保留管理员源与强制开关，联测owner覆盖隔离。
# 函数用途: 恢复基础配置已有的层级记录，不把owner层冒充部署层。
def _base_config_layers(config: object) -> tuple[dict[str, object], ...]:
    raw_layers = getattr(config, "config_layers", ()) if config is not None else ()
    if isinstance(raw_layers, list | tuple):
        records = tuple(item for item in raw_layers if isinstance(item, dict))
        if records:
            return records
    return (_LOADED_CONFIG_DEFAULT_SOURCE,)


# LLM: 运行层不能修改管理员强制开关；权限/来源规则仍沿原优先级，私网名单只收窄。
# 函数用途: 合并一层运行覆盖但保留宿主管理边界。
def _apply_runtime_config_layer(
    *,
    values: dict[str, Any],
    sources: dict[str, dict[str, object]],
    layer: ConfigLayer,
    allowed: set[str],
) -> None:
    for key, value in _runtime_layer_items(layer, allowed):
        if key == "force_compact_trigger_percent":
            continue
        if layer.priority < _current_priority(sources.get(key)):
            continue
        if key == "allowed_private_hosts":
            base_allowed = set(values.get("allowed_private_hosts") or ())
            narrowed = [item for item in (value or ()) if isinstance(item, str) and item in base_allowed]
            if not base_allowed and value:
                continue
            value = narrowed
        admin_default = sources.get(key, {}).get("compact_admin_default_percent", values.get(key))
        values[key] = value
        sources[key] = {"source": layer.source, "priority": layer.priority}
        if key == "memory_compact_trigger_percent":
            # 宿主原值只从base/原来源复制；运行覆盖值是用户默认，不能冒充管理员强制值。
            sources[key]["compact_admin_default_percent"] = admin_default


# LLM: 旧owner YAML比例映射到同一新键；新旧并列时只接受新键作生效比例，仍由原合并函数保留管理员原值。
# 函数用途: 枚举已允许的运行字段和兼容压缩键。
def _runtime_layer_items(layer: ConfigLayer, allowed: set[str]):
    for key, value in dict(layer.values).items():
        if key in allowed and value is not None:
            yield key, value
            if key == "memory_compact_auto_trigger_percent" and "memory_compact_trigger_percent" not in layer.values:
                from .config_layers import compact_policy_percent

                yield "memory_compact_trigger_percent", compact_policy_percent(value, 80)


# LLM: 来源来自已加载宿主 config，菜单 profile ID 是恢复引用；仅保留声明元数据，不复制未知秘密字段。
# 函数用途: 规范化配置来源时保留模型身份，避免加工具超时等无关配置后孙代理偷偷换回默认模型。
def _source_record(value: object) -> dict[str, object]:
    if isinstance(value, dict) and "source" in value:
        priority = _current_priority(value)
        record = {"source": str(value.get("source") or "unknown"), "priority": priority}
        if record["source"] == "owner_model_profile" and isinstance(value.get("profile_id"), str):
            record["profile_id"] = value["profile_id"]
        if type(value.get("compact_admin_default_percent")) is int:
            record["compact_admin_default_percent"] = value["compact_admin_default_percent"]
        return record
    return dict(_LOADED_CONFIG_DEFAULT_SOURCE)


# LLM: 作用域配置层；仅原优先级合并，保留管理员源与强制开关，联测owner覆盖隔离。
# 函数用途: 取得当前配置键的生效优先级，保持原层级顺序。
def _current_priority(source: object) -> int:
    if isinstance(source, dict):
        try:
            return int(source.get("priority", 20))
        except (TypeError, ValueError):
            return 20
    return 20


__all__ = [
    "RuntimeConfigLayer",
    "RuntimeConfigLayerRequest",
    "load_runtime_config_layer",
    "make_runtime_config_layer",
    "merge_runtime_config_layers",
    "runtime_scope_priority",
]
