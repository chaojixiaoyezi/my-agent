
from __future__ import annotations

"""Tool selection policy for scheduled hierarchy children."""

from dataclasses import dataclass
from typing import Any, ClassVar

from ...role_templates import COORDINATOR_TOOLS, role_template_snapshot_for_role

_DEFAULT_LEAF_CODING_TOOLS = [
    "list_files",
    "read_file",
    "search_text",
    "read_artifact",
    "web_search",
    "web_fetch",
    "write_file",
    "apply_patch",
    "inspect_agent_tree",
    "send_guidance",
    "raise_event",
    "raise_collaboration",
    "inspect_collaboration",
    "submit_collaboration_result",
    "update_collaboration",
    "capability_request",
]

_CHILD_CREATION_TOOLS = frozenset(
    {
        "schedule_child_subagents",
        "dispatch_subagents",
    }
)


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


def scheduled_child_tools(request: ToolPolicyRequest) -> list[str]:
    """Return a child capability set that can only narrow its parent's tools."""

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


def _leaf_write_tools(tools: list[str], *, parent_tools: list[str]) -> list[str]:
    return [
        tool
        for tool in _inherited_tools(tools, parent_tools)
        if tool not in _CHILD_CREATION_TOOLS
    ]


def _coordinator_tools(tools: list[str], *, parent_tools: list[str]) -> list[str]:
    return _inherited_tools(tools, parent_tools)


def _inherited_tools(tools: list[str], parent_tools: list[str]) -> list[str]:
    parent_scope = set(parent_tools)
    return [tool for tool in _clean_tools(tools) if tool in parent_scope]


def _clean_tools(value: object) -> list[str]:
    if not isinstance(value, list | tuple | set):
        return []
    return list(
        dict.fromkeys(
            str(item or "").strip()
            for item in value
            if str(item or "").strip()
        )
    )
