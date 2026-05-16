# LLM: Top-level subagent dispatch closeout helpers avoid extra final model calls after all work is verified.
# 模块用途: 当顶层主代理调度的子代理全部 DONE/VERIFIED 时，生成本地收尾回答，避免完成后再请求模型卡住。

from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar

from ..backends import ModelResponse
from ._runtime_params import ToolLoopExecuteParams
from .orchestration_run_scope import (
    remembered_dispatched_orchestration_run_ids,
    remembered_orchestration_run_ids,
)
from .subagent_dispatch_closeout_rendering import (
    dispatch_completion_text,
    dispatch_incomplete_notice,
    dispatch_limit_text,
    dispatch_missing_quality_roles_notice,
)
from .subagent_dispatch_closeout_resolution import (
    all_tasks_done_verified,
    blocking_task_ids,
)


# LLM: DispatchCompletionRequest bundles the deterministic closeout inputs for bundle-interface rules.
# 类用途: 集中保存顶层 dispatch 收口判断所需上下文；调用方只传一个参数包，避免扩散散参。
@dataclass(frozen=True)
class DispatchCompletionRequest:
    __test__: ClassVar[bool] = False

    agent: object
    params: ToolLoopExecuteParams
    before_executed_count: int
    backend: str


# LLM: subagent_dispatch_completion_response closes top-level dispatch without another model call.
# 函数用途: 顶层主代理刚完成 dispatch_subagents 且全部子代理 DONE/VERIFIED 时，直接生成确定性收尾说明。
def subagent_dispatch_completion_response(request: DispatchCompletionRequest) -> ModelResponse | None:
    if _inside_subagent_runner(request.agent) or not _round_executed_dispatch(request):
        return None
    tasks = _subagent_tasks(request.agent)
    if not tasks or not all_tasks_done_verified(tasks):
        return None
    if _prompt_requires_uncreated_quality_roles(request.params.user_prompt, tasks):
        return None
    return ModelResponse(text=dispatch_completion_text(tasks), backend=request.backend)


# LLM: subagent_dispatch_limit_response prevents final user reports from inventing subagent status.
# 函数用途: 顶层工具轮数耗尽时，直接按 task.json 生成事实状态报告，不再让模型自由总结失败链路。
def subagent_dispatch_limit_response(agent, *, backend: str, reason: str = "tool_limit") -> ModelResponse | None:
    if _inside_subagent_runner(agent):
        return None
    tasks = _subagent_tasks(agent)
    if not tasks:
        return None
    return ModelResponse(text=dispatch_limit_text(tasks, reason=reason), backend=backend)


# LLM: subagent_dispatch_final_response_guard makes the final user answer reflect persisted task state.
# 函数用途: 顶层主代理用过子代理编排工具后，如果 task.json 仍有阻塞，就用事实报告替换模型草稿，防止先报喜再纠正。
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
    prompt = str(getattr(agent, "_current_user_prompt", "") or "")
    missing_quality_roles = _missing_required_quality_roles(prompt, tasks)
    if missing_quality_roles:
        return ModelResponse(
            text=dispatch_missing_quality_roles_notice(tasks, missing_quality_roles),
            backend=response.backend,
        )
    blockers = blocking_task_ids(tasks)
    if not blockers:
        return response
    notice = dispatch_incomplete_notice(tasks, blockers)
    text = str(getattr(response, "text", "") or "")
    if text.strip() == notice.strip():
        return response
    return ModelResponse(text=notice, backend=response.backend)


# LLM: _has_closeout_scope keeps final answers tied to the persisted root-turn subagent scope.
# 函数用途: 判断最终回答是否需要按本轮子代理事实兜底；即使后面又读文件/search，也不能丢掉前面派工的阻塞状态。
def _has_closeout_scope(agent, executed_tools: list[object]) -> bool:
    return _executed_orchestration(executed_tools) or bool(remembered_dispatched_orchestration_run_ids(agent))


# LLM: _executed_orchestration mirrors the top-level tool-loop check without importing the service.
# 函数用途: 判断本轮是否执行过 dispatch；保留旧路径，兼容还没记录 run scope 的调用方。
def _executed_orchestration(executed_tools: list[object]) -> bool:
    return "dispatch_subagents" in [str(item or "") for item in executed_tools or []]


# LLM: _inside_subagent_runner keeps runner closeout controlled by output.json only.
# 函数用途: 判断当前是否处在某个子代理 runner 内；runner 内不能用顶层 dispatch 收口替代 output.json 契约。
def _inside_subagent_runner(agent) -> bool:
    return bool(str(getattr(agent, "_current_subagent_run_id", "") or ""))


# LLM: _round_executed_dispatch looks only at tools executed in the just-finished round.
# 函数用途: 判断刚结束的工具轮是否真实执行过 dispatch_subagents，避免普通看板查询被误当成收尾信号。
def _round_executed_dispatch(request: DispatchCompletionRequest) -> bool:
    return "dispatch_subagents" in [
        str(item or "") for item in request.params.executed_tools[request.before_executed_count:]
    ]


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


# LLM: _prompt_requires_uncreated_quality_roles preserves explicit tester/acceptor workflow contracts.
# 函数用途: 用户要求 worker 后继续创建测试/验收角色时，顶层不能因现有任务全绿而提前本地收口。
def _prompt_requires_uncreated_quality_roles(prompt: str, tasks: list[object]) -> bool:
    return bool(_missing_required_quality_roles(prompt, tasks))


# LLM: _missing_required_quality_roles turns prompt intent into concrete missing role tokens.
# 函数用途: 找出用户明确要求但尚未真实创建的 tester/acceptor 角色，防止 root 只靠口头总结跳过验收链路。
def _missing_required_quality_roles(prompt: str, tasks: list[object]) -> list[str]:
    required = _required_quality_roles(prompt)
    if not required:
        return []
    present = _present_role_tokens(tasks)
    return sorted(role for role in required if role not in present)


# LLM: _required_quality_roles reads only explicit role-style requirements, not generic quality prose.
# 函数用途: 从当前用户 prompt 判断是否明确要求 tester/acceptor 角色，避免普通“测试一下”误伤本地收口。
def _required_quality_roles(prompt: str) -> set[str]:
    text = " ".join(str(prompt or "").lower().split())
    if not text:
        return set()
    required: set[str] = set()
    if (
        "tester" in text
        or " qa " in f" {text} "
        or "测试子代理" in text
        or "测试代理" in text
        or "派测试" in text
    ):
        required.add("tester")
    if (
        "acceptor" in text
        or "验收子代理" in text
        or "验收代理" in text
        or "派验收" in text
    ):
        required.add("acceptor")
    return required


# LLM: _present_role_tokens normalizes role/name fields enough for deterministic closeout gating.
# 函数用途: 汇总已有子代理的 role 和名字关键词，判断 tester/acceptor 是否已经真正创建过。
def _present_role_tokens(tasks: list[object]) -> set[str]:
    present: set[str] = set()
    for task in tasks:
        text = " ".join(
            [
                str(getattr(task, "role", "") or ""),
                str(getattr(task, "agent_name", "") or ""),
            ]
        ).lower()
        if "tester" in text or "test" in text or "测试" in text:
            present.add("tester")
        if "acceptor" in text or "accept" in text or "验收" in text:
            present.add("acceptor")
    return present
