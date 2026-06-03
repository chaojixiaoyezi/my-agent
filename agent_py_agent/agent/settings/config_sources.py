from __future__ import annotations

import os
from dataclasses import fields
from pathlib import Path
from typing import Any

from .config_compat import INTERNAL_RUNTIME_CONFIG_FIELDS
from .config_layers import ConfigLayer, EffectiveConfig, merge_config_layers


def public_config_keys(config_cls: type) -> set[str]:
    return {item.name for item in fields(config_cls)} - INTERNAL_RUNTIME_CONFIG_FIELDS


def merge_agent_config_sources(
    *,
    config_cls: type,
    normalized: dict[str, object],
    raw_keys: set[str],
    path: Path,
) -> EffectiveConfig:
    allowed = public_config_keys(config_cls)
    clean = {key: normalized[key] for key in raw_keys if key in allowed and key in normalized}
    schema_defaults = _schema_default_values(config_cls, allowed)
    env_values = _env_override_values(clean, schema_defaults)
    layers = [
        ConfigLayer(source="schema_default", priority=0, values=schema_defaults),
        ConfigLayer(source=str(path.expanduser().resolve()), priority=20, values=clean),
    ]
    if env_values.values:
        layers.append(env_values)
    return merge_config_layers(layers, allowed_keys=allowed)


def _schema_default_values(config_cls: type, allowed: set[str]) -> dict[str, Any]:
    defaults = config_cls()
    return {key: getattr(defaults, key) for key in allowed}


def _env_override_values(clean: dict[str, object], schema_defaults: dict[str, object]) -> ConfigLayer:
    env_name = str(clean.get("api_key_env", schema_defaults.get("api_key_env", "")) or "").strip()
    if not env_name:
        return ConfigLayer(source="env", priority=80, values={})
    env_value = os.environ.get(env_name, "").strip()
    values = {"api_key": env_value} if env_value else {}
    return ConfigLayer(source=f"env:{env_name}", priority=80, values=values)


__all__ = ["merge_agent_config_sources", "public_config_keys"]
