# LLM: 用户配置只能进入 public_config_keys；宿主凭据来源、C已解析home快照与诊断字段均不可从 YAML 或任务覆盖注入。
# 模块用途: 合并配置来源优先级并隔离宿主内部字段，避免用户覆盖擦洗边界。
from __future__ import annotations

import os
from dataclasses import fields
from pathlib import Path
from typing import Any

from .config_layers import ConfigLayer, EffectiveConfig, compact_policy_percent, merge_config_layers

INTERNAL_RUNTIME_CONFIG_FIELDS = {
    "_compact_trigger_previous_auto",
    "config_layers",
    "config_path",
    "config_sources",
    "host_credential_env_names",
    "host_home_snapshot",
}


# LLM: 宿主专有和模型目录投影均不进入公开键；元数据只限定输入，不签发权限或重建运行秘密。
# 函数用途: 从配置模型取得对外公开的键，不把内部追踪字段当用户配置。
def public_config_keys(config_cls: type) -> set[str]:
    return {item.name for item in fields(config_cls) if not item.metadata.get("host_only", False)} - INTERNAL_RUNTIME_CONFIG_FIELDS


# LLM: 只合并正式 schema 的字段，来源优先级不变；退役字段不在运行时转换，用户字段仍排除宿主内部事实。
# 函数用途: 合并默认、配置文件和环境来源，不隐式执行旧配置迁移。
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


# LLM: 配置来源投影；只承接真实默认/环境层，不把派生记录当新授权，联测配置加载。
# 函数用途: 提取配置schema的默认值，作为最低优先级来源。
def _schema_default_values(config_cls: type, allowed: set[str]) -> dict[str, Any]:
    defaults = config_cls()
    return {key: getattr(defaults, key) for key in allowed}


# LLM: 配置来源投影；只承接真实默认/环境层，不把派生记录当新授权，联测配置加载。
# 函数用途: 读取受支持环境变量覆盖项，记录实际环境来源。
def _env_override_values(clean: dict[str, object], schema_defaults: dict[str, object]) -> ConfigLayer:
    env_name = str(clean.get("api_key_env", schema_defaults.get("api_key_env", "")) or "").strip()
    if not env_name:
        return ConfigLayer(source="env", priority=80, values={})
    env_value = os.environ.get(env_name, "").strip()
    values = {"api_key": env_value} if env_value else {}
    return ConfigLayer(source=f"env:{env_name}", priority=80, values=values)


__all__ = ["INTERNAL_RUNTIME_CONFIG_FIELDS", "merge_agent_config_sources", "public_config_keys"]
