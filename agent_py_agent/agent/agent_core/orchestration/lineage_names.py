from __future__ import annotations

from ...subagents.services.base import CreateRunParams

_PLACEHOLDER_AGENT_NAMES = {
    "",
    "general",
    "worker",
    "subagent",
    "agent",
    "child",
}
_DEFAULT_DEPTH = 1


def indexed_count_params(run_params: CreateRunParams, *, index: int, count: int) -> CreateRunParams:
    task_goal = f"{run_params.goal} / 子任务{index}" if count > 1 else run_params.goal
    task_name = indexed_agent_name(
        run_params.agent_name,
        role=run_params.role,
        index=index,
        require_index=count > 1,
    )
    return CreateRunParams(**{**run_params.__dict__, "goal": task_goal, "agent_name": task_name})


def indexed_item_params(run_params: CreateRunParams, *, index: int, total: int) -> CreateRunParams:
    task_name = indexed_agent_name(
        run_params.agent_name,
        role=run_params.role,
        index=index,
        require_index=total > 1,
    )
    return CreateRunParams(**{**run_params.__dict__, "agent_name": task_name})


def indexed_agent_name(agent_name: str, *, role: str, index: int, require_index: bool = False) -> str:
    name = str(agent_name or "").strip()
    if needs_system_lineage_name(name):
        return f"agent-d{_DEFAULT_DEPTH}-{role_suffix(role)}-{index}"
    if require_index and not has_trailing_identifier(name):
        return f"{name}-{index}"
    return name


def needs_system_lineage_name(agent_name: str) -> bool:
    text = str(agent_name or "").strip().strip("-").casefold()
    return text in _PLACEHOLDER_AGENT_NAMES


def role_suffix(role: str) -> str:
    suffix = str(role or "worker").strip().replace("_", "-").strip("-") or "worker"
    if suffix in {"general", "child", "subagent", "agent"}:
        return "worker"
    return suffix


def has_trailing_identifier(agent_name: str) -> bool:
    tail = str(agent_name or "").strip().rsplit("-", 1)[-1]
    return tail.isdigit()
