# LLM: 环境覆盖只合并正式用户配置键，不使用压缩 setter 补偿；重建时保留宿主凭据来源上界，不接收内部字段。
# 模块用途: 应用宿主声明的运行配置文件，保留来源诊断和子进程擦洗边界。
from __future__ import annotations

"""Runtime config overlays loaded from environment-provided scoped files."""

import os
from dataclasses import replace
from pathlib import Path

from ..config import AgentConfig, apply_log_level
from ..memory import normalize_agent_memory_config
from ..runtime_scope_config import (
    RuntimeConfigLayer,
    load_runtime_config_layer,
    merge_runtime_config_layers,
)
from ..secret_ref import freeze_managed_secret_environment

_ENV_RUNTIME_CONFIG = "MY_AGENT_RUNTIME_CONFIG"
_ENV_RUNTIME_CONFIG_LAYERS = "MY_AGENT_RUNTIME_CONFIG_LAYERS"
_ENV_RUNTIME_CONFIG_SCOPE = "MY_AGENT_RUNTIME_CONFIG_SCOPE"


# LLM: 无覆盖路径不改对象；有覆盖时直接使用 canonical 字段 replace，保留 base 来源上界，密钥值不另写盘。
# 函数用途: 合并明确的运行配置覆盖并应用原日志设置，后续 Shell 仍擦除原宿主环境来源。
def apply_runtime_config_environment(base_config: AgentConfig, *, env: dict[str, str] | None = None) -> AgentConfig:
    runtime_env = env if env is not None else os.environ
    layers = _runtime_config_layers_from_env(base_config, runtime_env)
    if not layers:
        return base_config
    effective = merge_runtime_config_layers(base_config, layers, config_cls=type(base_config))
    config = freeze_managed_secret_environment(replace(freeze_managed_secret_environment(base_config), **effective.values))
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


# LLM: 按环境变量声明顺序加载各作用域配置层，不修改传入基础配置。
# 函数用途: 从环境变量规格列表解析并读取运行配置覆盖层。
def _runtime_config_layers_from_env(config: AgentConfig, env: dict[str, str]) -> list[RuntimeConfigLayer]:
    specs = _layer_specs(env)
    layers: list[RuntimeConfigLayer] = []
    for scope, raw_path in specs:
        path = Path(raw_path).expanduser()
        layers.append(load_runtime_config_layer(path, scope=scope, config_cls=type(config)))
    return layers


# LLM: 多层环境变量优先于单文件环境变量，空值返回空列表。
# 函数用途: 提取环境变量中配置的(scope, path)覆盖层规格列表。
def _layer_specs(env: dict[str, str]) -> list[tuple[str, str]]:
    layered = str(env.get(_ENV_RUNTIME_CONFIG_LAYERS, "") or "").strip()
    if layered:
        return [_parse_layer_spec(item) for item in layered.split(",") if item.strip()]
    single = str(env.get(_ENV_RUNTIME_CONFIG, "") or "").strip()
    if not single:
        return []
    return [(str(env.get(_ENV_RUNTIME_CONFIG_SCOPE, "") or "runtime").strip() or "runtime", single)]


# LLM: 支持scope=path、scope:path与裸路径三种格式，缺省作用域为runtime。
# 函数用途: 解析单条运行配置层规格字符串为作用域与路径元组。
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


# LLM: 按层顺序聚合配置解析告警并加上来源前缀，不丢弃任何层内告警。
# 函数用途: 汇总全部运行配置覆盖层的告警列表。
def _runtime_config_warnings(layers: list[RuntimeConfigLayer]) -> list[str]:
    warnings: list[str] = []
    for layer in layers:
        warnings.extend(f"{layer.source}: {warning}" for warning in layer.warnings)
    return warnings


__all__ = ["apply_runtime_config_environment"]
