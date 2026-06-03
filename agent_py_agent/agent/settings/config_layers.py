from __future__ import annotations

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


__all__ = ["ConfigLayer", "EffectiveConfig", "merge_config_layers"]
