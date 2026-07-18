
from __future__ import annotations

from dataclasses import dataclass

from ..subagents import SubAgentTask
from ..subagents.role_templates import COORDINATOR_TOOLS, role_template_snapshot_for_role
from ..subagents.services.base import CreateRunParams
from .subagent.params import SpawnSubagentsParams


@dataclass(frozen=True)
class SpawnExplicitRoleRequest:
    agent: object
    options: SpawnSubagentsParams
    count: int
    allowed_tools: list[str] | None


@dataclass(frozen=True)
class ExplicitRoleRunRequest:
    seed: SpawnExplicitRoleRequest
    role: str
    allowed_tools: list[str] | None
    extra_roots: list[str]
    index: int


def clean_spawn_role(role: str) -> str:
    return str(role or "worker").strip().lower() or "worker"


def is_explicit_root_role(role: str, role_template_dirs: object = None) -> bool:
    return bool(role_template_snapshot_for_role(clean_spawn_role(role), role_template_dirs).get("can_spawn_children"))


def spawn_explicit_role_runs(request: SpawnExplicitRoleRequest) -> list[SubAgentTask]:
    role = clean_spawn_role(request.options.role)
    extra_roots = list(request.options.extra_write_roots or [])
    allowed_tools = _spawn_role_allowed_tools(role, request.allowed_tools)
    tasks: list[SubAgentTask] = []
    for index in range(1, request.count + 1):
        tasks.append(
            _create_explicit_role_run(
                ExplicitRoleRunRequest(
                    seed=request,
                    role=role,
                    allowed_tools=allowed_tools,
                    extra_roots=extra_roots,
                    index=index,
                )
            )
        )
    return tasks


def _spawn_role_allowed_tools(role: str, configured_tools: list[str] | None) -> list[str] | None:
    if not is_explicit_root_role(role):
        return configured_tools
    merged = [*COORDINATOR_TOOLS, *(configured_tools or [])]
    return list(dict.fromkeys(merged))


def _create_explicit_role_run(request: ExplicitRoleRunRequest) -> SubAgentTask:
    seed = request.seed
    return seed.agent.subagents.create_run(
        params=CreateRunParams(
            goal=_spawn_goal_text(seed.options.goal, request.index, seed.count),
            thought="负责拆分、调度、检查、接管和救援；权限覆盖下级，可按任务大小选择亲自完成或派工协作。",
            plan=["创建直接子代理", "调度直接子代理", "观察子代理状态", "汇报证据和阻塞"],
            agent_name=_spawn_agent_name(seed.options.agent_name, request.role, request.index, seed.count),
            role=request.role,
            allowed_tools=request.allowed_tools,
            extra_write_roots=request.extra_roots,
        )
    )


def _spawn_agent_name(base: str, role: str, index: int, total: int) -> str:
    root = str(base or "").strip() or clean_spawn_role(role).replace("_", "-")
    return root if total == 1 else f"{root}-{index}"


def _spawn_goal_text(goal: str, index: int, total: int) -> str:
    return str(goal or "") if total == 1 else f"{goal} / 子任务{index}"
