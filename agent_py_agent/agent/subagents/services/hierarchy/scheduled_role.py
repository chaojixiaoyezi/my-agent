
from __future__ import annotations

from typing import Any

from ...models import SubAgentTask
from ...role_contracts import normalize_subagent_role
from ...role_templates import role_template_id_for_role
from .tool_policy import LeafWriteIntentRequest, should_infer_leaf_coding_tools

_PLACEHOLDER_ROLES = {"", "general", "child"}
_PLACEHOLDER_AGENT_NAMES = {
    "",
    "worker",
    "general",
    "subagent",
    "agent",
    "child",
}


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
        return "worker"
    tools = set(getattr(spec, "allowed_tools", []) or [])
    if "create_subagents" not in tools:
        return role
    return _coordinator_role_for_depth(parent)


def scheduled_child_agent_name(parent: SubAgentTask, spec: Any, *, sibling_index: int = 1) -> str:
    depth = max(1, int(parent.depth or 0) + 1)
    raw_name = str(getattr(spec, "agent_name", "") or "").strip().strip("-")
    if raw_name and not is_placeholder_agent_name(raw_name) and not _is_placeholder_suffix(raw_name):
        return raw_name
    suffix = _agent_name_suffix(getattr(spec, "role", "worker"))
    if not agent_name_has_trailing_identifier(suffix):
        suffix = f"{suffix}-{max(1, int(sibling_index or 1))}"
    return f"agent-d{depth}-{suffix}"


def _agent_name_suffix(value: str, default: str = "worker") -> str:
    text = str(value or "").strip().replace("_", "-").strip("-") or "worker"
    default_text = str(default or "").strip().replace("_", "-").strip("-") or "worker"
    if _is_placeholder_suffix(text):
        return default_text
    return text


def is_placeholder_agent_name(value: str) -> bool:
    text = str(value or "").strip().strip("-").casefold()
    return text in _PLACEHOLDER_AGENT_NAMES


def agent_name_has_trailing_identifier(value: str) -> bool:
    return str(value or "").strip().rsplit("-", 1)[-1].isdigit()


def _is_placeholder_suffix(value: str) -> bool:
    text = str(value or "").strip().strip("-")
    return not text or "*" in text


def _coordinator_role_for_depth(parent: SubAgentTask) -> str:
    return "coordinator"


__all__ = [
    "agent_name_has_trailing_identifier",
    "child_context_manifest",
    "child_context_packs",
    "is_placeholder_agent_name",
    "role_from_child_spec_identity",
    "scheduled_child_agent_name",
    "scheduled_child_role",
]
