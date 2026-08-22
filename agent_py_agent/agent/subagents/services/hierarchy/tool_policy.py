
from __future__ import annotations

"""Tool selection policy for scheduled hierarchy children."""

from dataclasses import dataclass
from typing import Any, ClassVar

from ...role_templates import (
    COORDINATOR_TOOLS,
    DIRECT_CHILD_CONTROL_TOOLS,
    active_model_subagent_tools,
    role_template_snapshot_for_role,
)

_DEFAULT_LEAF_CODING_TOOLS = [
    "list_files",
    "read_file",
    "search_text",
    "read_artifact",
    "web_search",
    "web_fetch",
    "write_file",
    "apply_patch",
    "capability_request",
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


def _inherited_tools(tools: list[str], parent_tools: list[str]) -> list[str]:
    parent_scope = set(parent_tools)
    return [tool for tool in _clean_tools(tools) if tool in parent_scope]


# LLM: This seam delegates retirement and normalization to the single domain helper.
# 函数用途: 清理层级调度输入中的工具名并剔除旧控制入口。
def _clean_tools(value: object) -> list[str]:
    return active_model_subagent_tools(value)
