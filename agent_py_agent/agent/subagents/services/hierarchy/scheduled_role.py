
from __future__ import annotations

from typing import Any

from ...models import SubAgentTask
from ...role_contracts import normalize_subagent_role
from ...role_templates import role_template_id_for_role
from .tool_policy import LeafWriteIntentRequest, should_infer_leaf_coding_tools

_PLACEHOLDER_ROLES = {"", "general", "child"}


def child_context_manifest(parent: Any, spec: Any):
    if getattr(spec, "context_manifest", {}):
        return spec.context_manifest
    return getattr(parent, "context_manifest", None)


def child_context_packs(parent: Any, spec: Any):
    if getattr(spec, "context_packs", []):
        return spec.context_packs
    return getattr(parent, "context_packs", None)


def role_from_child_spec_identity(spec: Any) -> str:
    role = normalize_subagent_role(str(getattr(spec, "role", "") or "").strip())
    if role not in _PLACEHOLDER_ROLES:
        return role
    agent_name = normalize_subagent_role(str(getattr(spec, "agent_name", "") or "").strip())
    if agent_name not in _PLACEHOLDER_ROLES and role_template_id_for_role(agent_name):
        return agent_name
    return "worker"


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


__all__ = [
    "child_context_manifest",
    "child_context_packs",
    "role_from_child_spec_identity",
    "scheduled_child_role",
]
