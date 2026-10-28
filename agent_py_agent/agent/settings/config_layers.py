from __future__ import annotations

# LLM: 通用层合并保持原合同；压缩阈值以显式管理员/用户/线程输入解析，不按自然语言推断来源或授权。
# 模块用途: 合并带来源的配置层，并统一解析会话压缩比例的四级优先顺序。
"""Small config-layer merger with per-field source tracing.

This module is deliberately field-agnostic.  It only answers two questions:
which value wins, and which layer supplied it.  Owner/task/CLI policy code can
reuse it without adding a second business-specific config hierarchy.
"""

from collections.abc import Mapping
from dataclasses import asdict, dataclass, field, is_dataclass
from typing import Any


@dataclass(frozen=True)
class ConfigLayer:
    source: str
    priority: int
    values: Mapping[str, Any] = field(default_factory=dict)

    @classmethod
    def from_object(cls, *, source: str, priority: int, obj: object) -> ConfigLayer:
        return cls(source=source, priority=priority, values=_object_values(obj))


@dataclass(frozen=True)
class EffectiveConfig:
    values: dict[str, Any]
    sources: dict[str, dict[str, object]]
    layers: tuple[dict[str, object], ...]

    def value(self, key: str, default: Any = None) -> Any:
        return self.values.get(key, default)

    def source_for(self, key: str) -> dict[str, object]:
        return dict(self.sources.get(key, {}))

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": "effective_config.v1",
            "values": dict(self.values),
            "sources": dict(self.sources),
            "layers": list(self.layers),
        }


def merge_config_layers(
    layers: list[ConfigLayer] | tuple[ConfigLayer, ...],
    *,
    allowed_keys: set[str] | frozenset[str] | None = None,
    drop_none: bool = True,
) -> EffectiveConfig:
    effective: dict[str, Any] = {}
    sources: dict[str, dict[str, object]] = {}
    ordered = sorted(layers, key=lambda item: item.priority)
    for layer in ordered:
        for key, value in _iter_layer_items(layer, allowed_keys=allowed_keys, drop_none=drop_none):
            effective[key] = value
            sources[key] = {"source": layer.source, "priority": layer.priority}
    return EffectiveConfig(
        values=effective,
        sources=sources,
        layers=tuple({"source": item.source, "priority": item.priority} for item in ordered),
    )


def _iter_layer_items(
    layer: ConfigLayer,
    *,
    allowed_keys: set[str] | frozenset[str] | None,
    drop_none: bool,
):
    for key, value in dict(layer.values).items():
        if allowed_keys is not None and key not in allowed_keys:
            continue
        if drop_none and value is None:
            continue
        yield key, value


def _object_values(obj: object) -> dict[str, Any]:
    if isinstance(obj, Mapping):
        return dict(obj)
    if is_dataclass(obj) and not isinstance(obj, type):
        return asdict(obj)
    return {
        key: value
        for key in dir(obj)
        if not key.startswith("_")
        for value in [getattr(obj, key)]
        if not callable(value)
    }


# LLM: 百分比只接受严格整数，非法值使用调用方的明确缺省；此函数不读写配置或修改输入。
# 函数用途: 将生效压缩比例限制在50到95之间。
def compact_policy_percent(value: object, default: int | None = None) -> int | None:
    return min(95, max(50, value)) if type(value) is int else default


# LLM: 全局强制先于个人锁，锁定值只取管理员指定个人值或管理员默认；会话与用户覆盖不能解除锁。
# 函数用途: 返回四层压缩比例及全部来源字段，供运行时冻结和管理界面共用。
def resolve_compact_trigger_policy(*, admin_default_percent: object = 80, admin_forced: bool = False,
                                   thread_override_percent: object = None, user_policy: Mapping | None = None) -> dict[str, Any]:
    user_policy = user_policy if isinstance(user_policy, Mapping) else {}
    admin = compact_policy_percent(admin_default_percent, 80)
    user = compact_policy_percent(user_policy.get("default_trigger_percent"))
    thread = compact_policy_percent(thread_override_percent)
    locked = admin_forced is True or user_policy.get("force_locked") is True
    layers = [ConfigLayer("admin_default", 0, {"percent": admin}),
              ConfigLayer("user", 1, {"percent": user}),
              ConfigLayer("thread", 2, {"percent": thread})]
    if locked:
        layers.append(ConfigLayer("admin_forced", 3, {"percent": admin if admin_forced is True else user or admin}))
    effective = merge_config_layers(layers)
    return {"effective_trigger_percent": effective.value("percent"),
            "effective_source": effective.source_for("percent")["source"], "force_locked": locked,
            "user_default_percent": user, "thread_override_percent": thread, "admin_default_percent": admin}


# LLM: 配置规范化后的整数才参与裁决；直接构造的非法类型回默认，禁用0不被最小时长重新开启。
# 函数用途: 为运行时快照和Gateway维护提供同一闲置参数，不反向依赖agent_core。
def compact_idle_parameters(config: object) -> tuple[int, int]:
    threshold = getattr(config, "memory_compact_idle_trigger_percent", 60)
    seconds = getattr(config, "memory_compact_idle_seconds", 7200)
    threshold = threshold if type(threshold) is int else 60
    seconds = seconds if type(seconds) is int else 7200
    return min(95, max(0, threshold)), max(60, seconds) if seconds > 0 else 0


__all__ = ["ConfigLayer", "EffectiveConfig", "merge_config_layers", "resolve_compact_trigger_policy", "compact_idle_parameters"]
