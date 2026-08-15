
from __future__ import annotations

from dataclasses import dataclass


# LLM: 工具授权只接受机器 allowlist、owner 禁用表和默认隐藏表；空 capability 标签不参与判定。
# 类用途: 为注册表执行门提供最小且可审计的硬权限输入。
@dataclass(frozen=True)
class ToolAuthContext:
    allowed: set[str] | None
    disabled: set[str]
    default_hidden: set[str]


def registry_auth_error(
    tool_name: str,
    context: ToolAuthContext,
) -> str:
    return _tool_auth_error(tool_name, context)


def registry_auth_error_code(tool_name: str, context: ToolAuthContext) -> str:
    if tool_name in context.disabled:
        return "TOOL_NOT_ALLOWED"
    if context.allowed is None and tool_name in context.default_hidden:
        return "TOOL_NOT_ALLOWED"
    if context.allowed is not None and tool_name not in context.allowed:
        return "TOOL_NOT_ALLOWED"
    return ""


def allowed_tool_set(allowed_tools: list[str] | None) -> set[str] | None:
    if allowed_tools is None:
        return None
    return {str(item) for item in allowed_tools if str(item).strip()}


def _tool_auth_error(tool_name: str, context: ToolAuthContext) -> str:
    if tool_name in context.disabled:
        return f"工具被当前 owner 策略禁用: {tool_name}"
    if context.allowed is None and tool_name in context.default_hidden:
        return f"工具仅限内部显式授权: {tool_name}"
    if context.allowed is not None and tool_name not in context.allowed:
        return f"工具未授权: {tool_name}"
    return ""


__all__ = [
    "ToolAuthContext",
    "allowed_tool_set",
    "registry_auth_error",
    "registry_auth_error_code",
]
