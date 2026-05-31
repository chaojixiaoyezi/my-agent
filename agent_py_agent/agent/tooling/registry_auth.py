# LLM: Registry auth helpers keep tool visibility and security-tool grants centralized.
# 模块用途: 处理 allowed_tools、安全工具可见性和能力授权判断，不执行工具。

from __future__ import annotations

from dataclasses import dataclass

from ..log_analysis.capabilities import SECURITY_TOOL_NAMES, has_security_tool_capability


# LLM: ToolAuthContext carries the trusted fields needed for registry authorization.
# 类用途: 保存 allowlist、能力 grant 和安全工具可见性，供工具执行入口判断。
@dataclass(frozen=True)
class ToolAuthContext:
    allowed: set[str] | None
    disabled: set[str]
    granted_capabilities: list[str] | None
    expose_security_tools: bool
    security_tool_names: set[str]


# LLM: registry_auth_error returns a user-visible denial only after structured auth checks.
# 函数用途: 根据工具名、allowed_tools、capability grants 和安全工具集合判断是否允许调用。
def registry_auth_error(
    tool_name: str,
    context: ToolAuthContext,
) -> str:
    return _tool_auth_error(tool_name, context)


# LLM: allowed_tool_set normalizes optional allowed_tools into a set.
# 函数用途: 整理工具调用的 allowed_tool_set 信息，供注册表鉴权或执行使用。
def allowed_tool_set(allowed_tools: list[str] | None) -> set[str] | None:
    if allowed_tools is None:
        return None
    return {str(item) for item in allowed_tools if str(item).strip()}


# LLM: security_tools_visible decides whether security tools can appear in the catalog.
# 函数用途: 结合显式开关、allowed_tools 和 capability grants 判断安全工具是否可见。
def security_tools_visible(
    expose_security_tools: bool,
    *,
    allowed: set[str] | None,
    granted_capabilities: list[str] | None,
) -> bool:
    return bool(
        expose_security_tools
        or (allowed is not None and bool(allowed.intersection(SECURITY_TOOL_NAMES)))
        or has_security_tool_capability(granted_capabilities)
    )


# LLM: _tool_auth_error applies the concrete registry authorization rules.
# 函数用途: allowed_tools 先收窄普通工具，安全工具再额外要求可见性或 capability。
def _tool_auth_error(tool_name: str, context: ToolAuthContext) -> str:
    if tool_name in context.disabled:
        return f"工具被当前 owner 策略禁用: {tool_name}"
    if context.allowed is not None and tool_name not in context.allowed:
        return f"工具未授权: {tool_name}"
    if tool_name not in context.security_tool_names:
        return ""
    if _security_tool_call_authorized(
        tool_name,
        context.expose_security_tools,
        allowed=context.allowed,
        granted_capabilities=context.granted_capabilities,
    ):
        return ""
    return f"tool not authorized: {tool_name}"


# LLM: _security_tool_call_authorized checks the extra gate for security-sensitive tools.
# 函数用途: 安全工具需要 expose 开关、allowed_tools 或 capability grant 之一放行。
def _security_tool_call_authorized(
    tool_name: str,
    expose_security_tools: bool,
    *,
    allowed: set[str] | None,
    granted_capabilities: list[str] | None,
) -> bool:
    return bool(
        expose_security_tools
        or (allowed is not None and tool_name in allowed)
        or has_security_tool_capability(granted_capabilities)
    )


__all__ = ["ToolAuthContext", "allowed_tool_set", "registry_auth_error", "security_tools_visible"]
