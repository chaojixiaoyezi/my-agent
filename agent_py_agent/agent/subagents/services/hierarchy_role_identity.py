# LLM: Hierarchy role identity recovers concrete roles from model placeholder child specs.
# 模块用途: 当真实模型把 role 写成 child/general 时，根据 agent_name 和 goal 恢复内置角色。

from __future__ import annotations

from typing import Any

from ..role_contracts import normalize_subagent_role

_PLACEHOLDER_ROLES = {"", "general", "child"}
_ROLE_NAME_MARKERS = {
    "bug_finder": ("bug_finder", "bug-finder", "bugfinder", "bug finder"),
    "researcher": ("researcher", "research"),
    "tester": ("tester", "test"),
    "acceptor": ("acceptor", "acceptance"),
    "writer": ("writer", "writing"),
    "worker": ("worker", "implementer", "developer", "coder"),
    "coordinator": ("coordinator", "lead"),
}
_ROLE_GOAL_MARKERS = {
    "bug_finder": ("bug_finder", "bug-finder", "bug finder"),
    "researcher": ("researcher",),
    "tester": ("tester",),
    "acceptor": ("acceptor", "acceptance"),
    "writer": ("writer",),
    "worker": ("worker",),
    "coordinator": ("coordinator",),
}


# LLM: role_from_child_spec_identity keeps hierarchy scheduler resilient to model field drift.
# 函数用途: 模型把 role 写成 child 时，从 agent_name/goal 找真实角色，避免权限策略误把报告角色当产物角色。
def role_from_child_spec_identity(spec: Any) -> str:
    role = normalize_subagent_role(str(getattr(spec, "role", "") or "").strip())
    if role not in _PLACEHOLDER_ROLES:
        return role
    agent_name = str(getattr(spec, "agent_name", "") or "").lower()
    goal = str(getattr(spec, "goal", "") or "").lower()
    for candidate, markers in _ROLE_NAME_MARKERS.items():
        if any(marker in agent_name for marker in markers):
            return candidate
    for candidate, markers in _ROLE_GOAL_MARKERS.items():
        if any(marker in goal for marker in markers):
            return candidate
    return role or "worker"
