# LLM: 注册表、预设和派工共用发现与动作上界授权归一；创建快照持久化精确 tool:action 上界，子级永远不可放宽父级上界。
# 模块用途: 判定工具硬授权并保留精确动作上界，防止跨代、预设合并或能力申请时扩大权限。
from __future__ import annotations

from dataclasses import dataclass

from .versioned_input_decoder import business_domain_actions, retired_tool_aliases

CATALOG_ONLY_TOOL_GRANT = "tool_search:catalog"


def _parse_action_ceiling_token(item: str) -> tuple[str, str] | None:
    if ":" not in item:
        return None
    tool, sep, action = item.partition(":")
    if not sep or not tool or not action or ":" in action:
        return None
    if any(ch.isspace() for ch in tool) or any(ch.isspace() for ch in action):
        return None
    return tool, action


# LLM: 单一授权来源独立归一；保留显式空集合与目录只读语义，旧业务叶工具展开为裸域工具加单动作上界，孤立 tool:action 令牌不授予任何权限。
# 函数用途: 将原始授权项规范化为“裸工具名 + 精确动作上界令牌”的有序去重列表。
def normalize_tool_grants(tools: list[str]) -> list[str]:
    if not tools:
        return []
    cleaned = [
        str(item).strip()
        for item in tools
        if item is not None and str(item).strip()
    ]
    if not cleaned:
        return []
    aliases = retired_tool_aliases()
    has_explicit_tool_search = "tool_search" in cleaned
    bare_present: set[str] = set()
    for raw in cleaned:
        if raw in {"list_tools", CATALOG_ONLY_TOOL_GRANT}:
            bare_present.add("tool_search")
        elif ":" in raw:
            continue
        elif raw in aliases:
            bare_present.add(aliases[raw][0])
        else:
            bare_present.add(raw)

    result: list[str] = []
    seen: set[str] = set()
    pending_tokens: dict[str, list[str]] = {}

    def _emit(entry: str) -> None:
        if entry not in seen:
            seen.add(entry)
            result.append(entry)

    def _emit_bare(name: str) -> None:
        _emit(name)
        for pending in pending_tokens.pop(name, ()):
            _emit(f"{name}:{pending}")

    def _emit_or_queue_token(name: str, action: str) -> None:
        if name not in bare_present:
            return
        if name in seen:
            _emit(f"{name}:{action}")
        else:
            bucket = pending_tokens.setdefault(name, [])
            if action not in bucket:
                bucket.append(action)

    for raw in cleaned:
        if raw == "list_tools":
            _emit_bare("tool_search")
            if not has_explicit_tool_search:
                _emit_or_queue_token("tool_search", "catalog")
            continue
        if raw == CATALOG_ONLY_TOOL_GRANT:
            _emit_bare("tool_search")
            _emit_or_queue_token("tool_search", "catalog")
            continue
        if ":" in raw:
            parsed = _parse_action_ceiling_token(raw)
            if parsed is not None:
                _emit_or_queue_token(parsed[0], parsed[1])
            continue
        if raw in aliases:
            target, action = aliases[raw]
            _emit_bare(target)
            _emit_or_queue_token(target, action)
            continue
        _emit_bare(raw)
    return result


# LLM: 纯授权归一；合并角色或预设前冻结旧目录与业务叶窄意图，显式完整tool_search不因同存旧名误收窄，已有令牌始终优先。
# 函数用途: 将旧目录名与业务别名变为规范工具加动作限制，避免后续默认工具覆盖窄意图。
def canonical_discovery_grants(tools: list[str]) -> list[str]:
    return normalize_tool_grants(tools)


def action_ceilings(grants: list[str]) -> dict[str, frozenset[str]]:
    normalized = normalize_tool_grants(grants)
    bare = {item for item in normalized if ":" not in item}
    grouped: dict[str, set[str]] = {}
    for item in normalized:
        parsed = _parse_action_ceiling_token(item)
        if parsed is None or parsed[0] not in bare:
            continue
        grouped.setdefault(parsed[0], set()).add(parsed[1])
    return {tool: frozenset(actions) for tool, actions in grouped.items() if actions}


def intersect_tool_grants(child: list[str], parent: list[str]) -> list[str]:
    child_norm = normalize_tool_grants(child)
    parent_norm = normalize_tool_grants(parent)
    if not child_norm or not parent_norm:
        return []
    child_bare = [item for item in child_norm if ":" not in item]
    parent_bare = {item for item in parent_norm if ":" not in item}
    child_ceil = action_ceilings(child_norm)
    parent_ceil = action_ceilings(parent_norm)

    kept_bare: list[str] = []
    kept_tokens: list[str] = []
    for tool in child_bare:
        if tool not in parent_bare:
            continue
        c_acts = child_ceil.get(tool)
        p_acts = parent_ceil.get(tool)
        if c_acts is None and p_acts is None:
            kept_bare.append(tool)
            continue
        eff = (
            c_acts
            if p_acts is None
            else (p_acts if c_acts is None else (c_acts & p_acts))
        )
        if not eff:
            continue
        kept_bare.append(tool)
        kept_tokens.extend(f"{tool}:{act}" for act in sorted(eff))
    return [*kept_bare, *kept_tokens]


# LLM: 角色归一与能力回退共用的纯转换；只有tool_grants显式完整授权可证明search，旧v1名称证据保留目录但不扩动作，无IO。
# 函数用途: 将可信创建快照变为带动作上界的规范授权，防止各读取点对旧记录作不同解释。
def creation_snapshot_tool_grants(snapshot: dict[str, object]) -> list[str]:
    names = snapshot.get("available_tool_names")
    if not isinstance(names, list):
        return []
    raw_grants = snapshot.get("tool_grants")
    clean_names = [name.strip() for name in names if isinstance(name, str) and name.strip()]
    clean_grants = (
        [name.strip() for name in raw_grants if isinstance(name, str) and name.strip()]
        if isinstance(raw_grants, list)
        else []
    )
    names_norm = normalize_tool_grants(clean_names)
    grants_norm = normalize_tool_grants(clean_grants)
    scope = normalize_tool_grants([*names_norm, *grants_norm])
    if "tool_search" in scope and "tool_search" not in clean_grants and CATALOG_ONLY_TOOL_GRANT not in scope:
        scope.append(CATALOG_ONLY_TOOL_GRANT)
    return scope


# LLM: 只观察当前冻结schema及可见/就绪事实；catalog-only或没有search动作证据时不能依赖按名加载，不改变授权。
# 函数用途: 判断发现入口能否找回收起工具，供实际展示和自查投影共用。
def snapshot_search_can_load(snapshot) -> bool:
    runtime = snapshot.runtime("tool_search")
    return bool(runtime is not None and runtime.availability.available and runtime.exposure.model_visible
        and "search" in runtime.model_spec.input_schema.get("properties", {}).get("action", {}).get("enum", []))


# LLM: 从真实执行快照schema读取有效动作上界（含创建时未就绪动作的排除）；可用名字只是投影，不能替代可持久化动作约束；无IO。
# 函数用途: 把当前有效工具和精确动作上界压成派工可保存的授权列表。
def snapshot_tool_grants(snapshot) -> list[str]:
    available = sorted(snapshot.available_tool_names)
    raw_allowed = getattr(snapshot, "allowed_tools", None)
    snapshot_ceilings = action_ceilings(list(raw_allowed)) if raw_allowed is not None else {}
    domain_actions = business_domain_actions()
    tokens: list[str] = []
    for name in available:
        runtime = snapshot.runtime(name)
        enum_vals: list[str] | None = None
        if runtime is not None and isinstance(runtime.model_spec.input_schema, dict):
            props = runtime.model_spec.input_schema.get("properties")
            action_prop = props.get("action") if isinstance(props, dict) else None
            raw_enum = action_prop.get("enum") if isinstance(action_prop, dict) else None
            if isinstance(raw_enum, list):
                enum_vals = [str(v).strip() for v in raw_enum if isinstance(v, str) and str(v).strip()]
        full_actions: frozenset[str] | None = None
        if name == "tool_search":
            full_actions = frozenset({"search", "catalog"})
        elif name in domain_actions:
            full_actions = frozenset(domain_actions[name])
        elif runtime is not None:
            handler_spec = getattr(getattr(runtime, "handler", None), "model_spec", None)
            handler_schema = getattr(handler_spec, "input_schema", None)
            if isinstance(handler_schema, dict):
                h_props = handler_schema.get("properties")
                h_act = h_props.get("action") if isinstance(h_props, dict) else None
                h_enum = h_act.get("enum") if isinstance(h_act, dict) else None
                if isinstance(h_enum, list):
                    full_actions = frozenset(str(v).strip() for v in h_enum if isinstance(v, str) and str(v).strip())
        effective_actions: set[str] | None = None
        if enum_vals is not None and full_actions is not None and frozenset(enum_vals) < full_actions:
            effective_actions = set(enum_vals)
        if name in snapshot_ceilings:
            ceil_set = set(snapshot_ceilings[name])
            if effective_actions is not None:
                effective_actions &= ceil_set
            elif enum_vals is not None:
                effective_actions = set(enum_vals) & ceil_set
            else:
                effective_actions = ceil_set
        if effective_actions:
            tokens.extend(f"{name}:{act}" for act in sorted(effective_actions))
    return [*available, *dict.fromkeys(tokens)]


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


# LLM: 快照和展示求交共用规范授权名，保留精确 tool:action 上界令牌；孤立令牌不授予任何权限，无IO。
# 函数用途: 将显式白名单归一成集合，保留未限制与空授权的区别。
def allowed_tool_set(allowed_tools: list[str] | None) -> set[str] | None:
    if allowed_tools is None:
        return None
    return set(normalize_tool_grants([str(item) for item in allowed_tools if item is not None and str(item).strip()]))


def _tool_auth_error(tool_name: str, context: ToolAuthContext) -> str:
    if tool_name in context.disabled:
        return f"工具被当前 owner 策略禁用: {tool_name}"
    if context.allowed is None and tool_name in context.default_hidden:
        return f"工具仅限内部显式授权: {tool_name}"
    if context.allowed is not None and tool_name not in context.allowed:
        return f"工具未授权: {tool_name}"
    return ""


__all__ = [
    "CATALOG_ONLY_TOOL_GRANT",
    "ToolAuthContext",
    "action_ceilings",
    "allowed_tool_set",
    "canonical_discovery_grants",
    "creation_snapshot_tool_grants",
    "intersect_tool_grants",
    "normalize_tool_grants",
    "registry_auth_error",
    "registry_auth_error_code",
    "snapshot_search_can_load",
    "snapshot_tool_grants",
]
