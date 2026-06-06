
from __future__ import annotations

from dataclasses import dataclass

from ..log_analysis.capabilities import SECURITY_TOOL_NAMES, has_security_tool_capability


@dataclass(frozen=True)
class ToolAuthContext:
    allowed: set[str] | None
    disabled: set[str]
    default_hidden: set[str]
    granted_capabilities: list[str] | None
    expose_security_tools: bool
    security_tool_names: set[str]


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
    if tool_name in context.security_tool_names and not _security_tool_call_authorized(
        tool_name,
        context.expose_security_tools,
        allowed=context.allowed,
        granted_capabilities=context.granted_capabilities,
    ):
        return "TOOL_NOT_ALLOWED"
    return ""


def allowed_tool_set(allowed_tools: list[str] | None) -> set[str] | None:
    if allowed_tools is None:
        return None
    return {str(item) for item in allowed_tools if str(item).strip()}


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


def _tool_auth_error(tool_name: str, context: ToolAuthContext) -> str:
    if tool_name in context.disabled:
        return f"工具被当前 owner 策略禁用: {tool_name}"
    if context.allowed is None and tool_name in context.default_hidden:
        return f"工具仅限内部显式授权: {tool_name}"
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


__all__ = [
    "ToolAuthContext",
    "allowed_tool_set",
    "registry_auth_error",
    "registry_auth_error_code",
    "security_tools_visible",
]
