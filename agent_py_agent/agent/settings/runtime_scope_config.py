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
_LOADED_CONFIG_FALLBACK_SOURCE = {"source": "loaded_agent_config", "priority": 20}


@dataclass(frozen=True)
class RuntimeConfigLayer:
    scope: str
    source: str
    values: Mapping[str, Any] = field(default_factory=dict)
    priority: int | None = None
    warnings: tuple[str, ...] = ()

    def to_config_layer(self) -> ConfigLayer:
        return ConfigLayer(
            source=self.source,
            priority=runtime_scope_priority(self.scope, self.priority),
            values=self.values,
        )


@dataclass(frozen=True)
class RuntimeConfigLayerRequest:
    scope: str
    source: str
    values: Mapping[str, Any]
    config_cls: type
    priority: int | None = None


def runtime_scope_priority(scope: str, explicit: int | None = None) -> int:
    if explicit is not None:
        return int(explicit)
    return _SCOPE_PRIORITIES.get(str(scope or "").strip().lower(), 70)


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


def _base_config_values(config: object, allowed: set[str]) -> dict[str, Any]:
    return {key: getattr(config, key) for key in allowed if hasattr(config, key)}


def _base_config_sources(config: object, values: Mapping[str, Any]) -> dict[str, dict[str, object]]:
    raw_sources = getattr(config, "config_sources", {}) if config is not None else {}
    sources: dict[str, dict[str, object]] = {}
    for key in values:
        source = raw_sources.get(key) if isinstance(raw_sources, dict) else None
        sources[key] = _source_record(source)
    return sources


def _base_config_layers(config: object) -> tuple[dict[str, object], ...]:
    raw_layers = getattr(config, "config_layers", ()) if config is not None else ()
    if isinstance(raw_layers, list | tuple):
        records = tuple(item for item in raw_layers if isinstance(item, dict))
        if records:
            return records
    return (_LOADED_CONFIG_FALLBACK_SOURCE,)


def _apply_runtime_config_layer(
    *,
    values: dict[str, Any],
    sources: dict[str, dict[str, object]],
    layer: ConfigLayer,
    allowed: set[str],
) -> None:
    for key, value in _runtime_layer_items(layer, allowed):
        if layer.priority < _current_priority(sources.get(key)):
            continue
        values[key] = value
        sources[key] = {"source": layer.source, "priority": layer.priority}


def _runtime_layer_items(layer: ConfigLayer, allowed: set[str]):
    for key, value in dict(layer.values).items():
        if key in allowed and value is not None:
            yield key, value


def _source_record(value: object) -> dict[str, object]:
    if isinstance(value, dict) and "source" in value:
        priority = _current_priority(value)
        return {"source": str(value.get("source") or "unknown"), "priority": priority}
    return dict(_LOADED_CONFIG_FALLBACK_SOURCE)


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
