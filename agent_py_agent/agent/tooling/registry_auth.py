# LLM: 注册表、预设和派工共用发现授权归一；创建快照只有工具名不证明search动作，限制随allowed_tools持久化。
# 模块用途: 判定工具硬授权并保留只读目录动作上界，防止跨代或重建时恢复搜索权限。
from __future__ import annotations

from dataclasses import dataclass

CATALOG_ONLY_TOOL_GRANT = "tool_search:catalog"


# LLM: 纯授权归一；合并角色或预设前冻结旧目录窄意图，显式完整tool_search不因同存旧名误收窄，已有令牌始终优先。
# 函数用途: 将旧目录名变为规范工具加动作限制，避免后续默认工具覆盖只读意图。
def canonical_discovery_grants(tools: list[str]) -> list[str]:
    catalog_only = CATALOG_ONLY_TOOL_GRANT in tools or ("list_tools" in tools and "tool_search" not in tools)
    grants = list(dict.fromkeys("tool_search" if name == "list_tools" else name for name in tools))
    if catalog_only:
        if "tool_search" not in grants:
            grants.append("tool_search")
        if CATALOG_ONLY_TOOL_GRANT not in grants:
            grants.append(CATALOG_ONLY_TOOL_GRANT)
    return grants


# LLM: 角色归一与能力回退共用的纯转换；只有tool_grants显式完整授权可证明search，旧v1名称证据保留目录但不扩动作，无IO。
# 函数用途: 将可信创建快照变为带动作上界的规范授权，防止各读取点对旧记录作不同解释。
def creation_snapshot_tool_grants(snapshot: dict[str, object]) -> list[str]:
    names = snapshot.get("available_tool_names")
    if not isinstance(names, list):
        return []
    raw_grants = snapshot.get("tool_grants")
    grants = raw_grants if isinstance(raw_grants, list) else []
    scope = canonical_discovery_grants([name.strip() for name in [*names, *grants] if isinstance(name, str) and name.strip()])
    if "tool_search" in scope and "tool_search" not in grants and CATALOG_ONLY_TOOL_GRANT not in scope:
        scope.append(CATALOG_ONLY_TOOL_GRANT)
    return scope


# LLM: 只观察当前冻结schema及可见/就绪事实；catalog-only或没有search动作证据时不能依赖按名加载，不改变授权。
# 函数用途: 判断发现入口能否找回收起工具，供实际展示和自查投影共用。
def snapshot_search_can_load(snapshot) -> bool:
    runtime = snapshot.runtime("tool_search")
    return bool(runtime is not None and runtime.availability.available and runtime.exposure.model_visible
        and "search" in runtime.model_spec.input_schema.get("properties", {}).get("action", {}).get("enum", []))


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
