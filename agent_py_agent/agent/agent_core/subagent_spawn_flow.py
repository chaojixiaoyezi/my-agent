# LLM: Subagent spawn flow keeps delegation policy out of the lifecycle mixin.
# 模块用途: 统一处理 spawn_subagents 的自动拆分、固定数量拆分和显式 root/coordinator seed。

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .automation_guard import SubagentAutomationGuard
from .spawn_role_seed import (
    SpawnExplicitRoleRequest,
    is_explicit_root_role,
    spawn_explicit_role_runs,
)
from .subagent_params import SpawnSubagentsParams
from .task_complexity import estimate_task_complexity


# LLM: SpawnSubagentsFlowRequest bundles spawn flow state for a thin lifecycle facade.
# 类用途: 保存 spawn 流程需要的 agent、参数包和 workflow 模式，避免 mixin 方法继续变长。
@dataclass(frozen=True)
class SpawnSubagentsFlowRequest:
    agent: Any
    options: SpawnSubagentsParams
    workflow_mode: str


# LLM: spawn_subagents_flow routes explicit coordinator seed, auto delegation, and fixed-count split.
# 函数用途: 执行 spawn_subagents 的核心分支；普通 worker 兼容旧 split，显式 coordinator 创建 root seed。
def spawn_subagents_flow(request: SpawnSubagentsFlowRequest):
    if is_explicit_root_role(request.options.role):
        return _spawn_explicit_root_seed(request)
    if request.options.count is None:
        return _spawn_auto_delegated(request)
    return _spawn_fixed_count(request)


# LLM: configured_subagent_allowed_tools treats empty config as automatic role/tool policy.
# 函数用途: 从配置里取子代理工具白名单；空值返回 None，表示让角色模板和任务上下文自动判断工具。
def configured_subagent_allowed_tools(config: object) -> list[str] | None:
    value = getattr(config, "subagent_allowed_tools", [])
    if isinstance(value, str):
        raw_items = value.split(",")
    elif isinstance(value, (list, tuple)):
        raw_items = value
    else:
        return None
    tools = [str(item).strip() for item in raw_items if item is not None and str(item).strip()]
    return tools or None


# LLM: effective_max_subagents turns zero or invalid caps into a generous automatic limit.
# 函数用途: 解析子代理数量上限；0/坏值表示不限制用户意图，默认按 1000 这种宽松保护值处理。
def effective_max_subagents(value: object, *, fallback: int) -> int:
    try:
        cap = int(value)
    except (TypeError, ValueError):
        return fallback
    return cap if cap > 0 else fallback


# LLM: _spawn_explicit_root_seed creates one or more parent-capable coordinator roots.
# 函数用途: 创建显式 root/coordinator；不走 worker split，确保有调度工具且无写文件工具。
def _spawn_explicit_root_seed(request: SpawnSubagentsFlowRequest):
    count = min(
        request.options.count or 1,
        effective_max_subagents(request.agent.config.max_subagents, fallback=request.options.count or 1),
    )
    return spawn_explicit_role_runs(
        SpawnExplicitRoleRequest(
            agent=request.agent,
            options=request.options,
            count=count,
            allowed_tools=configured_subagent_allowed_tools(request.agent.config),
            workflow_mode=request.workflow_mode,
        )
    )


# LLM: _spawn_auto_delegated keeps previous complexity guard behavior for no-count worker spawn.
# 函数用途: 未指定 count 时按复杂度守卫决定是否自动拆分，并保留不拆分警告。
def _spawn_auto_delegated(request: SpawnSubagentsFlowRequest):
    allowed_tools = configured_subagent_allowed_tools(request.agent.config)
    complexity = estimate_task_complexity(
        request.options.goal,
        plan=[],
        allowed_tools=allowed_tools or [],
    )
    guard = SubagentAutomationGuard(request.agent.config)
    should_delegate = guard.should_delegate(complexity)
    tasks = _split_for_max_subagents(request, fallback=1000, allowed_tools=allowed_tools) if should_delegate else []
    guard.warn_if_not_delegating(complexity, bool(tasks))
    return tasks


# LLM: _spawn_fixed_count preserves explicit count behavior for regular worker splitting.
# 函数用途: 指定 count 时按 max_subagents 上限拆分普通 worker 子任务。
def _spawn_fixed_count(request: SpawnSubagentsFlowRequest):
    return _split_for_max_subagents(
        request,
        fallback=request.options.count or 1,
        allowed_tools=configured_subagent_allowed_tools(request.agent.config),
    )


# LLM: _split_for_max_subagents centralizes split count clamping and manager delegation.
# 函数用途: 根据配置上限计算实际数量，然后委托 SubAgentManager.split 创建普通子任务。
def _split_for_max_subagents(
    request: SpawnSubagentsFlowRequest,
    *,
    fallback: int,
    allowed_tools: list[str] | None,
):
    count = effective_max_subagents(request.agent.config.max_subagents, fallback=fallback)
    if request.options.count is not None:
        count = min(request.options.count, count)
    return request.agent.subagents.split(
        request.options.goal,
        count,
        workflow_mode=request.workflow_mode,
        allowed_tools=allowed_tools,
    )
