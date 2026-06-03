
from __future__ import annotations

from typing import Any

from ...models import SubAgentTask
from .role_identity import role_from_child_spec_identity
from .tool_policy import LeafWriteIntentRequest, should_infer_leaf_coding_tools


def scheduled_child_role(
    parent: SubAgentTask,
    spec: Any,
    extra_write_roots: list[str] | None = None,
    *,
    goal: str | None = None,
) -> str:
    role = role_from_child_spec_identity(spec)
    if role not in {"worker", "general", "child"}:
        return role
    if should_infer_leaf_coding_tools(
        LeafWriteIntentRequest(spec=spec, extra_write_roots=extra_write_roots or [], goal=goal)
    ):
        return "leaf_worker"
    tools = set(getattr(spec, "allowed_tools", []) or [])
    if "schedule_child_subagents" not in tools and "dispatch_subagents" not in tools:
        return role
    return _coordinator_role_for_depth(parent)


def _coordinator_role_for_depth(parent: SubAgentTask) -> str:
    depth = int(parent.depth or 0) + 1
    if depth == 1:
        return "child_coordinator"
    if depth == 2:
        return "grandchild_coordinator"
    return "coordinator"
