
from __future__ import annotations

from .model_task import SubAgentTask
from .root_task_policy import is_self_authorized_root_task


def runner_allowed_tools(task: SubAgentTask, tools: list[str]) -> list[str]:
    disabled = {
        str(item or "").strip()
        for item in (getattr(task, "effective_permissions", {}) or {}).get("disabled_tools", [])
        if str(item or "").strip()
    }
    filtered = [item for item in tools if str(item or "").strip() not in disabled]
    if not is_self_authorized_root_task(task):
        return filtered
    return [item for item in filtered if item != "capability_request"]


__all__ = ["runner_allowed_tools"]
