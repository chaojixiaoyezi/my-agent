# LLM: 默认值仍归 AgentConfig、CapabilityConfig、MemorySettings；本模块仅映射和读取，不维护第二份默认。
# 模块用途: 将各模块的决策设置投影成共用服务的字段，并保留默认来源。
from __future__ import annotations

import re
from collections.abc import Mapping

from .decision_settings_schema import GENERAL_FIELDS, POINT_FIELDS, POINTS, validate_decision_field


# LLM: 注册表只确定字段归属；能力点不能混入 AgentConfig，记忆点沿原记忆配置。
# 函数用途: 返回公共字段路径对应的原配置模块和字段名。
def decision_config_fields() -> dict[str, tuple[str, str]]:
    result = {key: ("agent", "decision_" + key) for key in GENERAL_FIELDS}
    for point in POINTS:
        domain = "capability" if point in {"subagent_model", "skill_tool"} else "memory" if point in {"recall", "curator"} else "agent"
        prefix = "memory_decision_" if domain == "memory" else "decision_"
        for field in POINT_FIELDS:
            result[f"points.{point}.{field}"] = (domain, f"{prefix}{point}_{field}")
    return result


# LLM: 原简化 YAML 把浮点/null 保留为字符串，此处显式转换后使用服务的严格校验，不允许布尔/NaN/零混入。
# 函数用途: 返回原配置中已填写决策字段的规范值，不修改传入对象。
def validate_config_decision_fields(values: Mapping, *, domain: str) -> dict:
    normalized = {}
    for path, (owner, field) in decision_config_fields().items():
        if (owner == domain or domain == "agent" and owner == "memory") and field in values:
            value = values[field]
            optional = path.startswith("points.") and not path.endswith(".mode")
            if optional and (value is None or value == "null"):
                normalized[field] = None
                continue
            if path.endswith("timeout_seconds") and type(value) is str and re.fullmatch(r"[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?", value):
                value = float(value)
            normalized[field] = validate_decision_field(path, value)
    return normalized


# LLM: 轻上下文可注入原 capability_config 或其原文件路径；没有 Agent 构造、provider 探测或持久写入。
# 函数用途: 获取各模块当前默认值和来源，点级空覆盖继续继承通用设置。
def decision_defaults(context: object) -> tuple[dict, dict]:
    from ..capability.config import CapabilityConfig, load_capability_config
    from ._memory_types import MemorySettings
    from .config import AgentConfig

    config = context.config
    capability = getattr(context, "capability_config", None)
    if capability is None:
        capability = getattr(getattr(context, "capability_router", None), "config", None)
    if capability is None and getattr(context, "capability_config_path", None):
        capability = load_capability_config(context.capability_config_path)
    defaults = {"agent": AgentConfig(), "capability": CapabilityConfig(), "memory": MemorySettings()}
    inputs = {"agent": config, "memory": config, "capability": capability or defaults["capability"]}
    values, sources = {}, {}
    for path, (domain, field) in decision_config_fields().items():
        value = getattr(inputs[domain], field, getattr(defaults[domain], field))
        if value is None and path.startswith("points.") and not path.endswith(".mode"):
            continue
        values[path] = validate_decision_field(path, value)
        sources[path] = f"{domain}_config.{field}"
    return values, sources
