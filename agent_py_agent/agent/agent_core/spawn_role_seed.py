# LLM: Explicit subagent role seed helpers keep root/coordinator creation out of lifecycle mixins.
# 模块用途: 为 `spawn-subagents --role coordinator` 创建真正可调度的 root/coordinator seed，避免默认 worker 入口污染层级 E2E。

from __future__ import annotations

from dataclasses import dataclass

from ..subagent import SubAgentTask
from ..subagents.role_templates import COORDINATOR_TOOLS
from ..subagents.services.base import CreateRunParams, _extract_write_dirs
from .subagent_params import SpawnSubagentsParams


# LLM: SpawnExplicitRoleRequest bundles explicit-root creation data to keep helper calls narrow.
# 类用途: 保存显式 role spawn 所需的 agent、参数、数量、工具和 workflow 模式。
@dataclass(frozen=True)
class SpawnExplicitRoleRequest:
    agent: object
    options: SpawnSubagentsParams
    count: int
    allowed_tools: list[str] | None
    workflow_mode: str


# LLM: ExplicitRoleRunRequest bundles one root seed creation item for parameter-count guard.
# 类用途: 保存单个显式 role 创建所需上下文，避免 helper 用散参数传递。
@dataclass(frozen=True)
class ExplicitRoleRunRequest:
    seed: SpawnExplicitRoleRequest
    role: str
    allowed_tools: list[str] | None
    extra_roots: list[str]
    index: int


# LLM: clean_spawn_role normalizes CLI/API role text without changing user-facing role ids.
# 函数用途: 把空 role 归一成 worker，并统一大小写，便于后续判断 coordinator/root 入口。
def clean_spawn_role(role: str) -> str:
    return str(role or "worker").strip().lower() or "worker"


# LLM: is_explicit_root_role detects roles that should be created as parent/coordinator nodes.
# 函数用途: 判断 spawn 是否需要创建能继续派孩子的 root/coordinator，而不是普通 worker split。
def is_explicit_root_role(role: str) -> bool:
    text = clean_spawn_role(role)
    return "coordinator" in text or text.endswith("lead")


# LLM: spawn_explicit_role_runs creates parent-capable seeds whose authority covers descendants.
# 函数用途: 创建显式 root/coordinator 任务；上层保留 goal 中的产物写根用于检查/接管/救援，但职责上仍优先派给下级执行。
def spawn_explicit_role_runs(request: SpawnExplicitRoleRequest) -> list[SubAgentTask]:
    role = clean_spawn_role(request.options.role)
    extra_roots = _extract_write_dirs(request.options.goal)
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


# LLM: _spawn_role_allowed_tools gives coordinator roots orchestration tools without dropping caller grants.
# 函数用途: 为显式 root/coordinator 补齐协调工具，并保留上层已授权的读写/执行工具。
def _spawn_role_allowed_tools(role: str, configured_tools: list[str] | None) -> list[str] | None:
    if not is_explicit_root_role(role):
        return configured_tools
    merged = [*COORDINATOR_TOOLS, *(configured_tools or [])]
    return list(dict.fromkeys(merged))


# LLM: _create_explicit_role_run builds one CreateRunParams bundle for deterministic root seeding.
# 函数用途: 创建单个显式 role 任务，保持 goal、agent_name、工具和继承写入根一致。
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
            workflow_mode=seed.workflow_mode,
        )
    )


# LLM: _spawn_agent_name keeps single-root names exact and multi-root names deterministic.
# 函数用途: 生成显式 role 的 agent_name；单个 root 保留用户名字，多个同类节点追加序号。
def _spawn_agent_name(base: str, role: str, index: int, total: int) -> str:
    root = str(base or "").strip() or clean_spawn_role(role).replace("_", "-")
    return root if total == 1 else f"{root}-{index}"


# LLM: _spawn_goal_text keeps explicit single roots from getting legacy child suffix noise.
# 函数用途: 单个 root/coordinator 使用原始 goal；批量创建时才追加编号，避免 E2E 提示词失真。
def _spawn_goal_text(goal: str, index: int, total: int) -> str:
    return str(goal or "") if total == 1 else f"{goal} / 子任务{index}"
