# LLM: Top-level subagent dispatch guards keep final answers aligned with persisted task state.
# 模块用途: 子代理调度工具本身只返回索引/状态；这里仅保留工具上限和最终回答的事实兜底。

from __future__ import annotations

from ..backends import ModelResponse
from ..subagents.role_templates import role_template_id_for_role
from ..subagents.services.qa_role_contract import qa_roles_required_by_task
from .orchestration_parent_acceptance_repair import parent_acceptance_rejected
from .orchestration_run_scope import (
    remembered_dispatched_orchestration_run_ids,
    remembered_orchestration_run_ids,
)
from .subagent_dispatch_closeout_rendering import (
    dispatch_incomplete_notice,
    dispatch_limit_text,
    dispatch_missing_quality_roles_notice,
)


# LLM: subagent_dispatch_limit_response prevents final user reports from inventing subagent status.
# 函数用途: 顶层工具轮数耗尽时，直接按 task.json 生成事实状态报告，不再让模型自由总结失败链路。
def subagent_dispatch_limit_response(agent, *, backend: str, reason: str = "tool_limit") -> ModelResponse | None:
    if _inside_subagent_runner(agent):
        return None
    if not _has_actual_dispatch_scope(agent):
        return None
    tasks = _subagent_tasks(agent)
    if not tasks:
        return None
    return ModelResponse(text=dispatch_limit_text(tasks, reason=reason), backend=backend)


# LLM: subagent_dispatch_final_response_guard keeps final answers aligned with persisted task state.
# 函数用途: 顶层主代理用过子代理编排工具后，如果 task.json 仍有真实失败/阻塞，就返回状态摘要，避免口头完成掩盖事实。
def subagent_dispatch_final_response_guard(
    agent,
    response: ModelResponse | None,
    *,
    executed_tools: list[object],
) -> ModelResponse | None:
    if response is None or _inside_subagent_runner(agent):
        return response
    if not _has_closeout_scope(agent, executed_tools):
        return response
    tasks = _subagent_tasks(agent)
    missing_quality_roles = _missing_required_quality_roles(tasks)
    if missing_quality_roles:
        return ModelResponse(
            text=dispatch_missing_quality_roles_notice(tasks, missing_quality_roles),
            backend=response.backend,
        )
    blockers = _repair_required_task_ids(tasks)
    if not blockers:
        return response
    notice = dispatch_incomplete_notice(tasks, blockers)
    text = str(getattr(response, "text", "") or "")
    if text.strip() == notice.strip():
        return response
    return ModelResponse(text=notice, backend=response.backend)


# LLM: _has_closeout_scope keeps final answers tied to the persisted root-turn subagent scope.
# 函数用途: 判断最终回答是否需要按本轮真实调度过的子代理事实兜底；dry-run/状态检查不能抢答。
def _has_closeout_scope(agent, executed_tools: list[object]) -> bool:
    dispatched = remembered_dispatched_orchestration_run_ids(agent)
    if dispatched:
        return True
    if remembered_orchestration_run_ids(agent):
        return _executed_orchestration(executed_tools)
    return _executed_orchestration(executed_tools)


# LLM: _has_actual_dispatch_scope separates real runner execution from dry-run/status dispatches.
# 函数用途: 工具上限和空响应兜底只有在 runner 真实跨过 dispatch 后才接管；无现代 scope 时保留旧 helper 兼容。
def _has_actual_dispatch_scope(agent) -> bool:
    if remembered_dispatched_orchestration_run_ids(agent):
        return True
    if remembered_orchestration_run_ids(agent):
        return False
    return True


# LLM: _executed_orchestration mirrors the top-level tool-loop check without importing the service.
# 函数用途: 判断本轮是否执行过 dispatch；保留旧路径，兼容还没记录 run scope 的调用方。
def _executed_orchestration(executed_tools: list[object]) -> bool:
    return "dispatch_subagents" in [str(item or "") for item in executed_tools or []]


# LLM: _inside_subagent_runner keeps runner closeout controlled by output.json only.
# 函数用途: 判断当前是否处在某个子代理 runner 内；runner 内不能用顶层 dispatch 收口替代 output.json 契约。
def _inside_subagent_runner(agent) -> bool:
    return bool(str(getattr(agent, "_current_subagent_run_id", "") or ""))


# LLM: _repair_required_task_ids distinguishes terminal repair facts from normal awaiting-acceptance work.
# 函数用途: 只有失败验收或阻塞类状态才触发本地未完成收口；普通待验收仍交给后续 dispatch/模型继续推进。
def _repair_required_task_ids(tasks: list[object]) -> list[str]:
    ids: list[str] = []
    for task in tasks:
        task_id = str(getattr(task, "id", "") or "")
        if task_id and (_task_status_requires_repair(task) or parent_acceptance_rejected(task)):
            ids.append(task_id)
    return ids


# LLM: _task_status_requires_repair captures generic lifecycle states that need recovery before success.
# 函数用途: BLOCKED/FAILED/TIMEOUT/CHANNEL_ERROR 这类终态不能继续等模型口头收尾，必须先修复或接管。
def _task_status_requires_repair(task: object) -> bool:
    return str(getattr(task, "status", "") or "").upper() in {
        "BLOCKED",
        "FAILED",
        "TIMEOUT",
        "CHANNEL_ERROR",
    }


# LLM: _subagent_tasks reads the manager list defensively for top-level closeout.
# 函数用途: 安全读取当前 workspace 的子代理任务列表；manager 不可用时保守返回空，不影响常规模型流程。
def _subagent_tasks(agent) -> list[object]:
    try:
        return _scoped_tasks(agent, list(agent.subagents.list_runs()))
    except Exception:
        return []


# LLM: _scoped_tasks avoids letting stale runs from a reused workspace pollute this turn's final report.
# 函数用途: 如果本轮记录了创建/调度过的 run_id，只汇总这些 run 及其同 root 子树；无记录时保持旧兼容行为。
def _scoped_tasks(agent, tasks: list[object]) -> list[object]:
    seen = remembered_orchestration_run_ids(agent)
    if not seen:
        return tasks
    root_ids = _scope_root_ids(tasks, seen)
    scoped = [
        task for task in tasks
        if _task_in_scope(task, seen, root_ids)
    ]
    return scoped


# LLM: _scope_root_ids expands explicit run ids to their persisted root ids for descendant closeout.
# 函数用途: 顶层只调度 root/coordinator 时，也能把它创建的子孙纳入同一轮收口。
def _scope_root_ids(tasks: list[object], seen: set[str]) -> set[str]:
    roots: set[str] = set()
    for task in tasks:
        task_id = str(getattr(task, "id", "") or "")
        if task_id not in seen:
            continue
        roots.add(str(getattr(task, "root_id", "") or task_id))
    return {item for item in roots if item}


# LLM: _task_in_scope keeps exact ids and same-root descendants, but excludes unrelated historical workspace runs.
# 函数用途: 判断 task 是否属于本轮创建/调度范围，避免 E2E 复用 workspace 时旧 run 进入 final guard。
def _task_in_scope(task: object, seen: set[str], root_ids: set[str]) -> bool:
    task_id = str(getattr(task, "id", "") or "")
    if task_id in seen:
        return True
    root_id = str(getattr(task, "root_id", "") or "")
    return bool(root_id and root_id in root_ids)


# LLM: _missing_required_quality_roles compares persisted task contracts to concrete role tokens.
# 函数用途: 找出父任务结构化要求但尚未真实创建的 tester/acceptor 角色，防止 root 只靠口头总结跳过验收链路。
def _missing_required_quality_roles(tasks: list[object]) -> list[str]:
    required = _required_quality_roles(tasks)
    if not required:
        return []
    present = _present_role_tokens(tasks)
    return sorted(role for role in required if role not in present)


# LLM: _required_quality_roles reads only task.attributes QA role contracts.
# 函数用途: 从本轮任务范围中读取 required_qa_roles / qa_roles，不解析用户 prompt 或任务 goal。
def _required_quality_roles(tasks: list[object]) -> set[str]:
    roles: set[str] = set()
    for task in tasks:
        roles.update(qa_roles_required_by_task(task))
    return roles


# LLM: _present_role_tokens normalizes role/name fields through template ids for deterministic closeout gating.
# 函数用途: 汇总已有子代理的 role 和名字模板 id，判断 tester/acceptor 是否已经真正创建过。
def _present_role_tokens(tasks: list[object]) -> set[str]:
    present: set[str] = set()
    for task in tasks:
        present.update(_quality_role_tokens_for_task(task))
    return present


# LLM: _quality_role_tokens_for_task keeps closeout role scanning shallow and template-id based.
# 函数用途: 从单个 task 的 role/agent_name 提取 tester/acceptor 模板角色，不读取目标自然语言。
def _quality_role_tokens_for_task(task: object) -> set[str]:
    values = (getattr(task, "role", ""), getattr(task, "agent_name", ""))
    return {
        role
        for value in values
        if (role := role_template_id_for_role(str(value or ""), fallback="")) in {"tester", "bug_finder", "acceptor"}
    }
