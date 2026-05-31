# LLM: Create-subagent config readers avoid persisting test doubles or hidden defaults.
# 模块用途: 读取子代理创建相关配置，保持默认值来自统一配置模型。

from __future__ import annotations

from ..settings.tool_config import DEFAULT_COMMAND_ACCESS_MODE


# LLM: config_access_mode reads the parent command access mode for child permission capping.
# 函数用途: 只从真实字符串配置读取 access_mode，缺失时回退统一命令权限默认值。
def config_access_mode(agent) -> str:
    value = getattr(getattr(agent, "config", None), "access_mode", DEFAULT_COMMAND_ACCESS_MODE)
    text = str(value).strip() if isinstance(value, str) else ""
    return text or DEFAULT_COMMAND_ACCESS_MODE


# LLM: config_string reads optional config strings without persisting test doubles.
# 函数用途: 只接受真实字符串，缺失或空值回退默认值。
def config_string(agent, key: str, default: str) -> str:
    value = getattr(getattr(agent, "config", None), key, default)
    text = str(value).strip() if isinstance(value, str) else ""
    return text or default


# LLM: config_int reads optional non-negative config integers.
# 函数用途: 将配置值转成非负整数，无法解析时回退默认值。
def config_int(agent, key: str, default: int) -> int:
    value = getattr(getattr(agent, "config", None), key, default)
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return max(0, parsed)


# LLM: config_bool reads optional config booleans from bool or common strings.
# 函数用途: 支持 true/false 字符串，其他类型回退默认值。
def config_bool(agent, key: str, default: bool) -> bool:
    value = getattr(getattr(agent, "config", None), key, default)
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"1", "true", "yes", "on"}:
            return True
        if lowered in {"0", "false", "no", "off"}:
            return False
    return default


__all__ = ["config_access_mode", "config_bool", "config_int", "config_string"]
