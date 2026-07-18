from __future__ import annotations

"""Runtime config overlays loaded from environment-provided scoped files."""

import os
from pathlib import Path

from ..config import AgentConfig, apply_log_level
from ..memory import normalize_agent_memory_config
from ..runtime_scope_config import (
    RuntimeConfigLayer,
    load_runtime_config_layer,
    merge_runtime_config_layers,
)

_ENV_RUNTIME_CONFIG = "MY_AGENT_RUNTIME_CONFIG"
_ENV_RUNTIME_CONFIG_LAYERS = "MY_AGENT_RUNTIME_CONFIG_LAYERS"
_ENV_RUNTIME_CONFIG_SCOPE = "MY_AGENT_RUNTIME_CONFIG_SCOPE"


def apply_runtime_config_environment(base_config: AgentConfig, *, env: dict[str, str] | None = None) -> AgentConfig:
    runtime_env = env if env is not None else os.environ
    layers = _runtime_config_layers_from_env(base_config, runtime_env)
    if not layers:
        return base_config
    effective = merge_runtime_config_layers(base_config, layers, config_cls=type(base_config))
    config = AgentConfig(**effective.values)
    config.config_path = base_config.config_path
    config.config_sources = effective.sources
    config.config_layers = list(effective.layers)
    config.config_warnings = [
        *list(getattr(base_config, "config_warnings", []) or []),
        *_runtime_config_warnings(layers),
    ]
    normalize_agent_memory_config(config)
    apply_log_level(config)
    return config


def _runtime_config_layers_from_env(config: AgentConfig, env: dict[str, str]) -> list[RuntimeConfigLayer]:
    specs = _layer_specs(env)
    layers: list[RuntimeConfigLayer] = []
    for scope, raw_path in specs:
        path = Path(raw_path).expanduser()
        layers.append(load_runtime_config_layer(path, scope=scope, config_cls=type(config)))
    return layers


def _layer_specs(env: dict[str, str]) -> list[tuple[str, str]]:
    layered = str(env.get(_ENV_RUNTIME_CONFIG_LAYERS, "") or "").strip()
    if layered:
        return [_parse_layer_spec(item) for item in layered.split(",") if item.strip()]
    single = str(env.get(_ENV_RUNTIME_CONFIG, "") or "").strip()
    if not single:
        return []
    return [(str(env.get(_ENV_RUNTIME_CONFIG_SCOPE, "") or "runtime").strip() or "runtime", single)]


def _parse_layer_spec(raw: str) -> tuple[str, str]:
    text = raw.strip()
    if "=" in text:
        scope, path = text.split("=", 1)
        return scope.strip() or "runtime", path.strip()
    if ":" in text:
        scope, path = text.split(":", 1)
        if scope.strip() and path.strip():
            return scope.strip(), path.strip()
    return "runtime", text


def _runtime_config_warnings(layers: list[RuntimeConfigLayer]) -> list[str]:
    warnings: list[str] = []
    for layer in layers:
        warnings.extend(f"{layer.source}: {warning}" for warning in layer.warnings)
    return warnings


__all__ = ["apply_runtime_config_environment"]
