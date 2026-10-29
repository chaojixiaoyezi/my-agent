# LLM: 注册表与派工共用宿主授权令牌；令牌不是可调用工具，不进入公共Schema，动作收窄随allowed_tools持久化。
# 模块用途: 判定工具硬授权并保留只读目录动作上界，防止跨代或重建时恢复搜索权限。
from __future__ import annotations

from dataclasses import dataclass

CATALOG_ONLY_TOOL_GRANT = "tool_search:catalog"


# LLM: 从真实执行快照schema读取动作上界；可用名字只是投影，不能替代可持久化动作约束；无IO。
# 函数用途: 把当前有效工具和只读目录限制压成派工可保存的授权列表。
def snapshot_tool_grants(snapshot) -> list[str]:
    grants = sorted(snapshot.available_tool_names)
    runtime = snapshot.runtime("tool_search")
    if runtime is not None and runtime.model_spec.input_schema.get("properties", {}).get("action", {}).get("enum") == ["catalog"]:
        grants.append(CATALOG_ONLY_TOOL_GRANT)
    return grants


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
