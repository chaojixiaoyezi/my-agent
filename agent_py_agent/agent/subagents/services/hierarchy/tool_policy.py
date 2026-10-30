# LLM: 层级调度只在父授权内收窄，同时迁移旧目录名并继承动作令牌；候选默认值不能增加父级工具或放宽search。
# 模块用途: 为层级调度生成的下级代理计算不超过父级上界的工具集合。
from __future__ import annotations

"""Tool selection policy for scheduled hierarchy children."""

from dataclasses import dataclass
from typing import Any, ClassVar

from ....tooling.registry_auth import CATALOG_ONLY_TOOL_GRANT, canonical_discovery_grants
from ....tooling.write_boundary import WRITE_TOOL_ORDER
from ...role_templates import (
    AGENT_PROGRESS_TOOLS,
    COORDINATOR_TOOLS,
    DIRECT_CHILD_CONTROL_TOOLS,
    RECORD_LESSON_TOOL,
    active_model_subagent_tools,
    role_template_snapshot_for_role,
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
    explicit_tools = _clean_tools(getattr(request.spec, "allowed_tools", None))
    requested_tools = explicit_tools or parent_tools
    if _can_spawn_children(request.role, request.spec):
        candidates = [*requested_tools, *_DEFAULT_LEAF_CODING_TOOLS, *COORDINATOR_TOOLS]
        return _coordinator_tools(candidates, parent_tools=parent_tools)
    candidates = [*requested_tools, *_DEFAULT_LEAF_CODING_TOOLS]
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
        if tool not in _DIRECT_CHILD_CONTROL_TOOLS
    ]


def _coordinator_tools(tools: list[str], *, parent_tools: list[str]) -> list[str]:
    return _inherited_tools(tools, parent_tools)


# LLM: 父级工具及动作限制同时求交；显式tool_search不覆盖父目录令牌，旧list_tools也须规范化而不是丢失目录。
# 函数用途: 为递归派工保留可持久化的最窄工具授权，不读自然语言或改运行记录。
def _inherited_tools(tools: list[str], parent_tools: list[str]) -> list[str]:
    parent_scope = set(_clean_tools(parent_tools))
    inherited = [tool for tool in _clean_tools(tools) if tool in parent_scope
        or (tool == CATALOG_ONLY_TOOL_GRANT and "tool_search" in parent_scope)]
    if "tool_search" in inherited and CATALOG_ONLY_TOOL_GRANT in parent_scope and CATALOG_ONLY_TOOL_GRANT not in inherited:
        inherited.append(CATALOG_ONLY_TOOL_GRANT)
    return inherited


# LLM: 先过原角色退休过滤，再复用发现授权归一；在默认候选混入完整工具名前保留旧目录窄意图，无副作用。
# 函数用途: 清理递归工具输入并迁移旧目录授权，避免升级记录丢工具或扩权。
def _clean_tools(value: object) -> list[str]:
    return canonical_discovery_grants(active_model_subagent_tools(value))
