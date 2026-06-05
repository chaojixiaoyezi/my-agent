
from __future__ import annotations

from typing import Any

from ...role_contracts import normalize_subagent_role
from ...role_templates import role_template_id_for_role

_PLACEHOLDER_ROLES = {"", "general", "child"}


def role_from_child_spec_identity(spec: Any) -> str:
    role = normalize_subagent_role(str(getattr(spec, "role", "") or "").strip())
    if role not in _PLACEHOLDER_ROLES:
        return role
    agent_name = normalize_subagent_role(str(getattr(spec, "agent_name", "") or "").strip())
    if agent_name not in _PLACEHOLDER_ROLES and role_template_id_for_role(agent_name):
        return agent_name
    return "worker"
