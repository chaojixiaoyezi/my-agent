
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
    "schedule_child_subagents",
    "dispatch_subagents",
    "inspect_agent_tree",
    "send_guidance",
    "raise_event",
    "raise_collaboration",
    "inspect_collaboration",
    "submit_collaboration_result",
    "update_collaboration",
    "capability_request",
]
_TOOL_NAME_ALIASES = {
    "list": "list_files",
    "read": "read_file",
    "patch": "apply_patch",
    "replace": "apply_patch",
    "search": "search_text",
    "write": "write_file",
}
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


def scheduled_child_tools(request: ToolPolicyRequest) -> list[str]:
    """Return the allowed tools for a scheduled child."""

    if request.spec.allowed_tools:
        explicit_tools = [_canonical_tool_name(item) for item in request.spec.allowed_tools]
        if is_coordinator_spec(request.spec):
            return _coordinator_tools([*explicit_tools, *_DEFAULT_LEAF_CODING_TOOLS, *COORDINATOR_TOOLS])
        return _leaf_write_tools([*explicit_tools, *_DEFAULT_LEAF_CODING_TOOLS])
    if is_coordinator_spec(request.spec):
        return _coordinator_tools([*request.parent_tools, *_DEFAULT_LEAF_CODING_TOOLS, *COORDINATOR_TOOLS])
    return _leaf_write_tools([*request.parent_tools, *_DEFAULT_LEAF_CODING_TOOLS])


def should_infer_leaf_coding_tools(request: LeafWriteIntentRequest) -> bool:
    if not request.extra_write_roots:
        return False
    if is_coordinator_spec(request.spec):
        return False
    return True


def is_coordinator_spec(spec: Any) -> bool:
    snapshot = role_template_snapshot_for_role(str(getattr(spec, "role", "") or ""))
    return bool(snapshot.get("can_spawn_children"))


def _leaf_write_tools(tools: list[str]) -> list[str]:
    return list(dict.fromkeys(tools))


def _coordinator_tools(tools: list[str]) -> list[str]:
    return list(dict.fromkeys(tools))


def _canonical_tool_name(tool_name: object) -> str:
    text = str(tool_name or "").strip()
    return _TOOL_NAME_ALIASES.get(text, text)
