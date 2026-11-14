# LLM: 层级调度只在父授权内收窄，同时迁移旧目录名并继承动作令牌；候选默认值不能增加父级工具或放宽search。
# 模块用途: 为层级调度生成的下级代理计算不超过父级上界的工具集合。
from __future__ import annotations

"""Tool selection policy for scheduled hierarchy children."""

from dataclasses import dataclass
from typing import Any, ClassVar

from ....tooling.registry_auth import intersect_tool_grants, normalize_tool_grants
from ....tooling.write_boundary import WRITE_TOOL_ORDER
from ...role_templates import (
    AGENT_PROGRESS_TOOLS,
    COORDINATOR_TOOLS,
    DIRECT_CHILD_CONTROL_TOOLS,
    RECORD_LESSON_TOOL,
    active_model_subagent_tools,
    role_template_snapshot_for_role,
    template_for_role,
)

# LLM: Recursive leaf defaults share the canonical write-tool order with direct
# children; scheduled_child_tools still intersects them with the parent's cap.
# 常量用途: 定义递归编码子代理的缺省工具候选，实际授权仍不得超过父级工具上界。
_DEFAULT_LEAF_CODING_TOOLS = [
    *AGENT_PROGRESS_TOOLS,
    "list_files",
    "read_file",
    "search_text",
    "read_artifact",
    "web_search",
    "web_fetch",
    *WRITE_TOOL_ORDER,
    "capability_request",
    RECORD_LESSON_TOOL,
]

_DIRECT_CHILD_CONTROL_TOOLS = frozenset(DIRECT_CHILD_CONTROL_TOOLS)


@dataclass(frozen=True)
class LeafWriteIntentRequest:
    """Inputs for deciding whether a scheduled child is a product-writing leaf."""

    __test__: ClassVar[bool] = False

    spec: Any
    extra_write_roots: list[str]
    goal: str | None = None


@dataclass(frozen=True)
class ToolPolicyRequest:
    """Inputs for deriving allowed tools for a scheduled child."""

    __test__: ClassVar[bool] = False

    parent_tools: list[str]
    spec: Any
    extra_write_roots: list[str]
    goal: str | None = None
    role: str = ""


# LLM: Hierarchy scheduling may only narrow an already filtered parent snapshot;
# explicit legacy tool names are removed before leaf/coordinator role logic.
# 函数用途: 为孙代理计算不超过父级且不含退休控制入口的工具集合。
def scheduled_child_tools(request: ToolPolicyRequest) -> list[str]:
    parent_tools = _clean_tools(request.parent_tools)
    spec_attrs = getattr(request.spec, "attributes", None)
    constraints_dict = spec_attrs.get("delegation_constraints") if isinstance(spec_attrs, dict) else None

    permitted_tools: list[str] | None = None
    prohibited_actions: tuple[str, ...] = ()
    if isinstance(constraints_dict, dict):
        raw_perm = constraints_dict.get("permitted_tools")
        if raw_perm:
            for tool in raw_perm:
                single = _clean_tools([tool])
                if not single or intersect_tool_grants(single, parent_tools) != single:
                    raise PermissionError(
                        f"TOOL_PERMISSION_DENIED: Permitted tool '{tool}' exceeds parent authority."
                    )
            permitted_tools = intersect_tool_grants(_clean_tools(raw_perm), parent_tools)
        prohibited_actions = tuple(constraints_dict.get("prohibited_actions") or ())

    raw_explicit = getattr(request.spec, "allowed_tools", None)
    explicit_tools = _clean_tools(raw_explicit)
    if raw_explicit is not None and not explicit_tools:
        raw_items = list(raw_explicit) if isinstance(raw_explicit, (list, tuple)) else [raw_explicit]
        if any(str(item or "").strip() for item in raw_items):
            return []
    template = template_for_role(request.role or str(getattr(request.spec, "role", "") or ""))
    narrow_defaults = template is not None and not template.inherit_base_tools
    # 窄角色的显式空清单也是上界，不能由默认候选重新补回。
    requested_tools = (explicit_tools if raw_explicit is not None
                       else _clean_tools(list(template.default_tools))) if narrow_defaults else (explicit_tools or parent_tools)

    if permitted_tools is not None:
        candidates = intersect_tool_grants(permitted_tools, requested_tools) if narrow_defaults else permitted_tools
    elif narrow_defaults:
        candidates = requested_tools
    elif _can_spawn_children(request.role, request.spec):
        candidates = _merge_preserving_explicit_ceilings(requested_tools, [*_DEFAULT_LEAF_CODING_TOOLS, *COORDINATOR_TOOLS])
    else:
        candidates = _merge_preserving_explicit_ceilings(requested_tools, _DEFAULT_LEAF_CODING_TOOLS)

    if "spawn_children" in prohibited_actions:
        candidates = [t for t in candidates if t.partition(":")[0] not in _DIRECT_CHILD_CONTROL_TOOLS and t.partition(":")[0] != "create_subagents"]
    if "network_access" in prohibited_actions:
        candidates = [t for t in candidates if t.partition(":")[0] not in ("web_search", "web_fetch", "network_fetch")]
    if "network_fetch" in prohibited_actions:
        candidates = [t for t in candidates if t.partition(":")[0] not in ("web_fetch", "network_fetch")]
    if "shell_execution" in prohibited_actions:
        candidates = [t for t in candidates if t.partition(":")[0] not in ("run_command", "process_session", "terminal_session")]

    if _can_spawn_children(request.role, request.spec) and "spawn_children" not in prohibited_actions:
        return _coordinator_tools(candidates, parent_tools=parent_tools)
    return _leaf_write_tools(candidates, parent_tools=parent_tools)


def should_infer_leaf_coding_tools(request: LeafWriteIntentRequest) -> bool:
    if not request.extra_write_roots:
        return False
    if is_coordinator_spec(request.spec):
        return False
    return True


def is_coordinator_spec(spec: Any) -> bool:
    snapshot = role_template_snapshot_for_role(str(getattr(spec, "role", "") or ""))
    return bool(snapshot.get("can_spawn_children"))


def _can_spawn_children(role: str, spec: Any) -> bool:
    snapshot = role_template_snapshot_for_role(str(role or ""))
    return bool(snapshot.get("can_spawn_children")) or is_coordinator_spec(spec)


# LLM: Scheduled leaf creation follows the same role boundary as direct
# create_subagents: every direct-child control is removed, not only spawn.
# 函数用途: 将层级调度生成的普通执行代理收窄为纯执行工具集合。
def _leaf_write_tools(tools: list[str], *, parent_tools: list[str]) -> list[str]:
    return [
        tool
        for tool in _inherited_tools(tools, parent_tools)
        if tool.partition(":")[0] not in _DIRECT_CHILD_CONTROL_TOOLS
    ]


def _coordinator_tools(tools: list[str], *, parent_tools: list[str]) -> list[str]:
    return _inherited_tools(tools, parent_tools)


# LLM: 父级工具及动作限制同时求交；显式tool_search或域工具不覆盖父动作上界，旧list_tools与业务别名同步规范化。
# 函数用途: 为递归派工保留可持久化的最窄工具授权，不读自然语言或改运行记录。
def _inherited_tools(tools: list[str], parent_tools: list[str]) -> list[str]:
    return intersect_tool_grants(_clean_tools(tools), _clean_tools(parent_tools))


def _merge_preserving_explicit_ceilings(explicit: list[str], defaults: list[str]) -> list[str]:
    from ....tooling.registry_auth import action_ceilings

    exp = _clean_tools(explicit)
    base = _clean_tools(defaults)
    exp_bare = {item for item in exp if ":" not in item}
    exp_ceil = action_ceilings(exp)
    base_ceil = action_ceilings(base)
    bare_order = [*(item for item in exp if ":" not in item), *(item for item in base if ":" not in item and item not in exp_bare)]
    tokens: list[str] = []
    for name in bare_order:
        acts = exp_ceil.get(name) if name in exp_bare else base_ceil.get(name)
        if acts:
            tokens.extend(f"{name}:{act}" for act in sorted(acts))
    return [*bare_order, *tokens]


# LLM: 先过原角色退休过滤，再复用授权归一；在默认候选混入完整工具名前保留窄动作上界，无副作用。
# 函数用途: 清理递归工具输入并迁移旧目录与业务叶授权，避免升级记录丢工具或扩权。
def _clean_tools(value: object) -> list[str]:
    return normalize_tool_grants(active_model_subagent_tools(value))
