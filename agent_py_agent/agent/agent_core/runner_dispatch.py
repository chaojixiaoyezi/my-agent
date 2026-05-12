# LLM: Agent core orchestration module; keep planning, dispatch, tool-loop, and finalization contracts stable.
# 模块用途: 支撑主代理运行循环、计划、工具调用、子代理调度和收尾。

from __future__ import annotations

"""selects runner candidates, handles retry policy, creates dispatch records, and runs worker agents.

dispatch 阶段不应该把"谁能跑、能不能重试、并发 worker 怎么启动"都塞在一个大函数里。
这个文件专门处理 runner 相关的规则和小工具。
"""

from dataclasses import dataclass
from typing import TYPE_CHECKING

from ..subagent import SubAgentRunnerResult, SubAgentTask
from ..subagents.services.dispatch_params import DispatchRecordParams
from .runner_patch_review import _dispatch_patch_review_run_ids, _task_has_runner_patches
from .runner_worker import RunSubagentWorkerParams, _run_subagent_worker
from .runner_workflow_dependencies import workflow_dependency_ready_candidates

if TYPE_CHECKING:
    from ..core import SimpleAgent


RETRYABLE_RUNNER_FAILURE_TYPES = {
    "runner_error",
    "structured_output_parse_error",
    "tool_result_missing",
    "model_error",
    "api_error",
    "transient_error",
    "runner_timeout",
}
_RUNNER_CHILD_FINAL_STATUSES = {"DONE", "FAILED", "TIMEOUT", "CHANNEL_ERROR", "TAKEN_OVER"}


# LLM: role phase ordering trusts role/name identity before broad inherited goal prose.
# 函数用途: 给 runner 角色分配执行阶段；coordinator 先拆任务，worker 产出，tester/找错随后检查，acceptor 最后验收。
def _runner_role_phase_priority(task: SubAgentTask) -> int:

    role = str(getattr(task, "role", "") or "").strip().lower().replace("-", "_")
    identity_text = f"{role} {getattr(task, 'agent_name', '')}".lower().replace("-", "_")
    full_text = f"{identity_text} {getattr(task, 'goal', '')}".lower().replace("-", "_")
    if not role:
        return _runner_text_phase_priority(full_text)
    if "coordinator" in role or role in {"lead", "planner", "dispatcher"}:
        return _runner_text_phase_priority(identity_text, default=0)
    if "accept" in identity_text or any(token in identity_text for token in {"acceptor", "verifier", "verification"}):
        return 30
    if (
        "quality" in identity_text
        or "test" in identity_text
        or "bug" in identity_text
        or "review" in identity_text
        or "critic" in identity_text
        or any(token in identity_text for token in {"qa", "checker", "auditor"})
    ):
        return 20
    if "worker" in role or role in {"general", "writer", "coder", "researcher", "reporter"}:
        return 10
    return _runner_text_phase_priority(full_text)


# LLM: _runner_text_phase_priority is a fallback for vague roles, not the main source for coordinators.
# 函数用途: 身份字段不够明确时，从文本里保守推断 runner 阶段，避免继承的父级 QA 合同污染 coordinator。
def _runner_text_phase_priority(text: str, *, default: int = 10) -> int:
    normalized = str(text or "").lower().replace("-", "_")
    if "accept" in normalized or any(token in normalized for token in {"acceptor", "verifier", "verification"}):
        return 30
    if (
        "quality" in normalized
        or "test" in normalized
        or "bug" in normalized
        or "review" in normalized
        or "critic" in normalized
        or any(token in normalized for token in {"qa", "checker", "auditor"})
    ):
        return 20
    return default


# LLM: _runner_max_attempts 属于 SimpleAgent 核心运行的函数边界；调整时先确认运行循环、工具调用、调度记录和最终响应仍按原契约工作。
# 函数用途: 推进执行器maxattempts的运行阶段，串接调度、等待、回写或错误处理；关键副作用: 会影响运行循环、工具调用、调度记录和最终响应，需保持重试、超时和状态迁移语义。
def _runner_max_attempts(policy: str) -> int:

    value = str(policy or "auto").strip().lower()
    if value in {"", "auto"}:
        return 2
    if value in {"off", "none", "disabled", "false", "no"}:
        return 1
    try:
        return max(1, int(value))
    except ValueError:
        return 2


# LLM: _runner_failure_type 属于 SimpleAgent 核心运行的函数边界；调整时先确认运行循环、工具调用、调度记录和最终响应仍按原契约工作。
# 函数用途: 推进执行器失败type的运行阶段，串接调度、等待、回写或错误处理；关键副作用: 会影响运行循环、工具调用、调度记录和最终响应，需保持重试、超时和状态迁移语义。
def _runner_failure_type(task: SubAgentTask) -> str:

    return str(task.failure_type or "").strip().lower()


# LLM: _runner_retry_reason 属于 SimpleAgent 核心运行的函数边界；调整时先确认运行循环、工具调用、调度记录和最终响应仍按原契约工作。
# 函数用途: 推进执行器retryreason的运行阶段，串接调度、等待、回写或错误处理；关键副作用: 会影响运行循环、工具调用、调度记录和最终响应，需保持重试、超时和状态迁移语义。
def _runner_retry_reason(task: SubAgentTask, runner_max_attempts: int) -> str:

    if runner_max_attempts <= 1:
        return ""
    if task.status not in {"BLOCKED", "FAILED", "TIMEOUT"}:
        return ""
    failure_type = _runner_failure_type(task)
    if failure_type not in RETRYABLE_RUNNER_FAILURE_TYPES:
        return ""
    attempts = max(0, int(task.runner_attempts or 0))
    if attempts >= runner_max_attempts:
        return ""
    return f"failure_type={failure_type}; attempt={attempts + 1}/{runner_max_attempts}"


# LLM: _resolve_runner_concurrency 属于 SimpleAgent 核心运行的函数边界；调整时先确认运行循环、工具调用、调度记录和最终响应仍按原契约工作。
# 函数用途: 读取或查询执行器concurrency需要的状态，返回调用方可继续处理的快照；关键副作用: 会影响运行循环、工具调用、调度记录和最终响应，需保持重试、超时和状态迁移语义。
def _resolve_runner_concurrency(value: object, job_count: int) -> int:

    if job_count <= 0:
        return 0
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"", "auto"}:
            return 1
        try:
            parsed = int(normalized)
        except ValueError:
            return 1
    else:
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            return 1
    return max(1, min(parsed, job_count))


# LLM: _resolve_runner_start_rate 属于 SimpleAgent 核心运行的函数边界；调整时先确认运行循环、工具调用、调度记录和最终响应仍按原契约工作。
# 函数用途: 读取或查询执行器startrate需要的状态，返回调用方可继续处理的快照；关键副作用: 会影响运行循环、工具调用、调度记录和最终响应，需保持重试、超时和状态迁移语义。
def _resolve_runner_start_rate(value: object, job_count: int) -> int:

    if job_count <= 0:
        return 0
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"", "auto"}:
            return job_count
        try:
            parsed = int(normalized)
        except ValueError:
            return job_count
    else:
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            return job_count
    return max(0, min(parsed, job_count))


# LLM: _resolve_runner_timeout_seconds 属于 SimpleAgent 核心运行的函数边界；调整时先确认运行循环、工具调用、调度记录和最终响应仍按原契约工作。
# 函数用途: 读取或查询执行器超时seconds需要的状态，返回调用方可继续处理的快照；关键副作用: 会影响运行循环、工具调用、调度记录和最终响应，需保持重试、超时和状态迁移语义。
def _resolve_runner_timeout_seconds(value: object) -> float:

    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"", "auto", "off", "none", "disabled", "false", "no"}:
            return 0.0
        try:
            parsed = float(normalized)
        except ValueError:
            return 0.0
    else:
        try:
            parsed = float(value)
        except (TypeError, ValueError):
            return 0.0
    return max(0.0, parsed)


# LLM: RunnerDispatchRecordParams 属于 SimpleAgent 核心运行的类边界；调整时先确认运行循环、工具调用、调度记录和最终响应仍按原契约工作。
# 类用途: 集中保存执行器调度记录参数字段，让调用方按同一参数包传递上下文；关键副作用: 本身不执行输入输出；字段变化会影响构造点、序列化和测试读取。
@dataclass(frozen=True)
class RunnerDispatchRecordParams:
    agent: SimpleAgent
    run_id: str
    before: SubAgentTask
    after: SubAgentTask
    result: SubAgentRunnerResult
    retry_reason: str
    execute_runners: bool


# LLM: _runner_dispatch_record 属于 SimpleAgent 核心运行的函数边界；调整时先确认运行循环、工具调用、调度记录和最终响应仍按原契约工作。
# 函数用途: 推进执行器调度记录的运行阶段，串接调度、等待、回写或错误处理；关键副作用: 会改动运行循环、工具调用、调度记录和最终响应，调用方依赖写入顺序和文件格式。
def _runner_dispatch_record(params: RunnerDispatchRecordParams):

    return params.agent.subagents.make_dispatch_record(
        params=DispatchRecordParams(
            step="runner",
            action=(
                "retry_runner"
                if params.retry_reason and params.execute_runners
                else "execute_runner"
                if params.execute_runners
                else "runner_dry_run"
            ),
            run_id=params.run_id,
            dry_run=params.result.dry_run,
            applied=not params.result.dry_run,
            ok=params.result.ok,
            message=params.result.message,
            before_status=params.before.status,
            after_status=params.after.status,
            before_verification_status=params.before.verification_status,
            after_verification_status=params.after.verification_status,
            evidence_paths=[
                params.result.execution_context_json,
                params.result.result_json,
                params.result.output_json,
            ],
            **_runner_child_summary_fields(params.agent, params.after, params.result),
        ),
    )


# LLM: _runner_child_summary_fields makes nested schedule_child_subagents visible to the parent dispatch payload.
# 函数用途: 从执行后的任务快照收集 child ids/roles 和 runner 摘要，避免上层模型把 dispatch 记录数当成孩子数。
def _runner_child_summary_fields(agent, after: SubAgentTask, result: SubAgentRunnerResult) -> dict[str, object]:
    child_ids = [str(item) for item in (after.child_ids or []) if str(item).strip()]
    child_states = _runner_child_states(agent, child_ids)
    return {
        "runner_summary": result.structured_summary,
        "runner_created_child_count": len(child_ids),
        "runner_created_child_ids": child_ids,
        "runner_created_roles": [item["role"] for item in child_states if item["role"]],
        "runner_child_status_counts": _runner_child_status_counts(child_states),
        "runner_unfinished_child_ids": _runner_unfinished_child_ids(child_states),
        "runner_partial_success": bool(child_ids and not result.ok),
    }


# LLM: _runner_child_states resolves child status/role from persisted refs only.
# 函数用途: 给 dispatch 报告附加轻量 child 状态；读失败时保留 id，避免调度记录写入失败。
def _runner_child_states(agent, child_ids: list[str]) -> list[dict[str, str]]:
    states: list[dict[str, str]] = []
    for child_id in child_ids:
        try:
            child = agent.subagents.load(child_id)
        except Exception:
            states.append({"id": child_id, "role": "", "status": "UNKNOWN"})
            continue
        states.append({
            "id": child_id,
            "role": str(getattr(child, "role", "") or "").strip(),
            "status": str(getattr(child, "status", "") or "UNKNOWN").strip().upper(),
        })
    return states


# LLM: _runner_child_status_counts keeps partial-success records compact.
# 函数用途: 汇总 runner 已创建 child 的状态分布，不读取 child artifact 正文。
def _runner_child_status_counts(child_states: list[dict[str, str]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for item in child_states:
        status = item["status"] or "UNKNOWN"
        counts[status] = counts.get(status, 0) + 1
    return counts


# LLM: _runner_unfinished_child_ids surfaces children that need another dispatch wave.
# 函数用途: 标出还没终态的 child ids，便于父级 timeout 后继续调度或接管。
def _runner_unfinished_child_ids(child_states: list[dict[str, str]]) -> list[str]:
    return [
        item["id"] for item in child_states
        if item["id"] and item["status"] not in _RUNNER_CHILD_FINAL_STATUSES
    ]


# LLM: _dispatch_runner_candidates 属于 SimpleAgent 核心运行的函数边界；调整时先确认运行循环、工具调用、调度记录和最终响应仍按原契约工作。
# 函数用途: 推进执行器candidates的运行阶段，串接调度、等待、回写或错误处理；关键副作用: 会影响运行循环、工具调用、调度记录和最终响应，需保持重试、超时和状态迁移语义。
def _dispatch_runner_candidates(
    tasks: list[SubAgentTask],
    max_runners: int,
    *,
    runner_max_attempts: int = 1,
) -> list[SubAgentTask]:

    if max_runners <= 0:
        return []
    candidates: list[SubAgentTask] = []
    for task in tasks:
        if not _is_dispatch_runner_candidate(task, runner_max_attempts=runner_max_attempts):
            continue
        candidates.append(task)
    candidates = workflow_dependency_ready_candidates(candidates, tasks)
    candidates = _ready_phase_candidates(candidates)
    ordered = sorted(enumerate(candidates), key=lambda item: (_runner_role_phase_priority(item[1]), item[0]))
    return [task for _, task in ordered[:max_runners]]


# LLM: _ready_phase_candidates prevents QA/test runners from racing ahead of implementation runners.
# 函数用途: 同一 dispatch 范围内只放行当前最低阶段的候选；生产线未完成前，quality/test/acceptance 先等待下一轮。
def _ready_phase_candidates(candidates: list[SubAgentTask]) -> list[SubAgentTask]:
    if not candidates:
        return []
    current_phase = min(_runner_role_phase_priority(task) for task in candidates)
    return [task for task in candidates if _runner_role_phase_priority(task) == current_phase]


# LLM: _limit_items 属于 SimpleAgent 核心运行的函数边界；调整时先确认运行循环、工具调用、调度记录和最终响应仍按原契约工作。
# 函数用途: 计算限制条目的预算、数量或限制，影响后续调度节奏；关键副作用: 需保持运行循环、工具调用、调度记录和最终响应上的返回值和副作用边界稳定。
def _limit_items(items: list, limit: int) -> list:

    if limit <= 0:
        return list(items)
    return list(items)[:limit]


# LLM: _is_dispatch_runner_candidate 属于 SimpleAgent 核心运行的函数边界；调整时先确认运行循环、工具调用、调度记录和最终响应仍按原契约工作。
# 函数用途: 判断执行器candidate条件是否成立，作为后续调度或分支决策的门禁；关键副作用: 会影响运行循环、工具调用、调度记录和最终响应，需保持重试、超时和状态迁移语义。
def _is_dispatch_runner_candidate(
    task: SubAgentTask,
    *,
    runner_max_attempts: int = 1,
) -> bool:

    if task.status in {
        "AWAITING_ACCEPTANCE",
        "DONE",
        "CHANNEL_ERROR",
        "TAKEN_OVER",
    }:
        return False
    if task.verification_status in {"NEEDS_ACCEPTANCE", "VERIFIED"}:
        return False
    if task.channel_status == "BROKEN":
        return False
    if any(item.status == "OPEN" for item in task.capability_requests):
        return False
    if any(item.status == "OPEN" for item in task.capability_gaps):
        return False
    if task.status == "BLOCKED":
        if _runner_failure_type(task) == "capability_request" and bool(task.capability_grants):
            return True
        return bool(_runner_retry_reason(task, runner_max_attempts))
    if task.status in {"FAILED", "TIMEOUT"}:
        return bool(_runner_retry_reason(task, runner_max_attempts))
    return task.status in {"PLANNING", "RUNNING"}
