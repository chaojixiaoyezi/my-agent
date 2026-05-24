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
from ..subagents.role_templates import role_template_id_for_role
from ..subagents.services.dispatch_params import DispatchRecordParams
from .runner_child_summary import runner_child_summary_fields
from .runner_input_dependencies import input_dependency_ready_candidates
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
    "provider_timeout",
    "runner_timeout",
}

CAPABILITY_GRANTED_BLOCKER_FAILURE_TYPES = {
    "capability_request",
    "permission_blocked",
    "missing_capability",
    "write_permission_blocked",
}

DEFAULT_AUTO_RUNNER_CONCURRENCY = 8


# LLM: role phase ordering trusts structured role/name identity only.
# 函数用途: 给 runner 角色分配执行阶段；coordinator 先拆任务，worker 产出，tester/bug_finder 随后检查，acceptor 最后验收。
def _runner_role_phase_priority(task: SubAgentTask) -> int:

    role = str(getattr(task, "role", "") or "").strip().lower().replace("-", "_")
    identity_text = f"{role} {getattr(task, 'agent_name', '')}".lower().replace("-", "_")
    template_role = role_template_id_for_role(identity_text, fallback="")
    if _identity_requests_quality_phase(identity_text):
        return 20
    if template_role == "coordinator" or role in {"lead", "planner", "dispatcher"}:
        return 0
    if template_role == "acceptor" or role in {"acceptor", "verifier", "verification"}:
        return 30
    if template_role in {"tester", "bug_finder"} or role in {"tester", "bug_finder", "qa", "checker", "auditor"}:
        return 20
    if template_role in {"worker", "writer", "researcher"} or role in {"worker", "writer", "researcher", "general", "coder", "reporter"}:
        return 10
    return 10


# LLM: _identity_requests_quality_phase lets structured QA names wait for producer phases.
# 函数用途: 当角色名/代理名明确是 quality/test/check 时，即使 role=coordinator，也归入质量阶段。
def _identity_requests_quality_phase(identity_text: str) -> bool:
    markers = ("quality", "qa", "tester", "test", "bug_finder", "checker", "acceptance")
    return any(marker in identity_text for marker in markers)


# LLM: _runner_text_phase_priority is retained for compatibility and delegates to template ids.
# 函数用途: 兼容旧测试入口；不从 goal 自然语言猜阶段，只按模板 id/结构化角色词判断。
def _runner_text_phase_priority(text: str, *, default: int = 10) -> int:
    normalized = str(text or "").lower().replace("-", "_")
    template_role = role_template_id_for_role(normalized, fallback="")
    if template_role == "acceptor" or normalized in {"verifier", "verification"}:
        return 30
    if template_role in {"tester", "bug_finder"} or normalized in {"qa", "checker", "auditor"}:
        return 20
    return default


# LLM: _runner_max_attempts 属于 SimpleAgent 核心运行的函数边界；调整时先确认运行循环、工具调用、调度记录和最终响应仍按原契约工作。
# 函数用途: 推进执行器maxattempts的运行阶段，串接调度、等待、回写或错误处理；关键副作用: 会影响运行循环、工具调用、调度记录和最终响应，需保持重试、超时和状态迁移语义。
def _runner_max_attempts(policy: str) -> int:

    value = str("auto" if policy is None else policy).strip().lower()
    if value in {"", "auto"}:
        return 2
    if value in {"off", "none", "disabled", "false", "no"}:
        return 1
    try:
        return max(0, int(value))
    except ValueError:
        return 2


# LLM: _runner_failure_type 属于 SimpleAgent 核心运行的函数边界；调整时先确认运行循环、工具调用、调度记录和最终响应仍按原契约工作。
# 函数用途: 推进执行器失败type的运行阶段，串接调度、等待、回写或错误处理；关键副作用: 会影响运行循环、工具调用、调度记录和最终响应，需保持重试、超时和状态迁移语义。
def _runner_failure_type(task: SubAgentTask) -> str:

    return str(task.failure_type or "").strip().lower()


# LLM: _runner_retry_reason 属于 SimpleAgent 核心运行的函数边界；调整时先确认运行循环、工具调用、调度记录和最终响应仍按原契约工作。
# 函数用途: 推进执行器retryreason的运行阶段，串接调度、等待、回写或错误处理；关键副作用: 会影响运行循环、工具调用、调度记录和最终响应，需保持重试、超时和状态迁移语义。
def _runner_retry_reason(task: SubAgentTask, runner_max_attempts: int) -> str:

    if runner_max_attempts == 1:
        return ""
    if task.status not in {"BLOCKED", "FAILED", "TIMEOUT"}:
        return ""
    failure_type = _runner_failure_type(task)
    if failure_type not in RETRYABLE_RUNNER_FAILURE_TYPES:
        return ""
    attempts = max(0, int(task.runner_attempts or 0))
    if runner_max_attempts > 0 and attempts >= runner_max_attempts:
        return ""
    max_attempts_label = "unlimited" if runner_max_attempts <= 0 else str(runner_max_attempts)
    return f"failure_type={failure_type}; attempt={attempts + 1}/{max_attempts_label}"


# LLM: _resolve_runner_concurrency 属于 SimpleAgent 核心运行的函数边界；调整时先确认运行循环、工具调用、调度记录和最终响应仍按原契约工作。
# 函数用途: 读取或查询执行器concurrency需要的状态，返回调用方可继续处理的快照；关键副作用: 会影响运行循环、工具调用、调度记录和最终响应，需保持重试、超时和状态迁移语义。
def _resolve_runner_concurrency(value: object, job_count: int) -> int:

    if job_count <= 0:
        return 0
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"", "auto"}:
            return min(job_count, DEFAULT_AUTO_RUNNER_CONCURRENCY)
        try:
            parsed = int(normalized)
        except ValueError:
            return min(job_count, DEFAULT_AUTO_RUNNER_CONCURRENCY)
    else:
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            return min(job_count, DEFAULT_AUTO_RUNNER_CONCURRENCY)
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
            **runner_child_summary_fields(params.agent, params.after, params.result),
        ),
    )


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
    candidates = input_dependency_ready_candidates(candidates)
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

    if task.status == "RUNNING":
        return False
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
        if _blocked_after_capability_grant(task):
            return True
        return bool(_runner_retry_reason(task, runner_max_attempts))
    if task.status in {"FAILED", "TIMEOUT"}:
        return bool(_runner_retry_reason(task, runner_max_attempts))
    return task.status == "PLANNING"


# LLM: granted capability blockers should rerun once the parent has routed the request.
# 函数用途: 能力申请已被父级授权且没有未处理 OPEN 请求时，允许 blocked runner 再跑一轮，避免“纸面授权但任务卡死”。
def _blocked_after_capability_grant(task: SubAgentTask) -> bool:
    if not getattr(task, "capability_grants", None):
        return False
    if any(item.status == "OPEN" for item in getattr(task, "capability_requests", []) or []):
        return False
    if any(item.status == "OPEN" for item in getattr(task, "capability_gaps", []) or []):
        return False
    if _has_fresh_capability_grant(task):
        return True
    return _runner_failure_type(task) in CAPABILITY_GRANTED_BLOCKER_FAILURE_TYPES


# LLM: _has_fresh_capability_grant makes rerun eligibility a state contract, not a failure-word allowlist.
# 函数用途: 父级在上次 runner 后新增授权时，允许同一个 BLOCKED run 续跑一次；续跑后不再凭旧 grant 无限重跑。
def _has_fresh_capability_grant(task: SubAgentTask) -> bool:
    last_attempt = _float_attr(task, "runner_last_attempt_at")
    if last_attempt <= 0:
        return False
    for grant in getattr(task, "capability_grants", []) or []:
        if _float_attr(grant, "created_at") > last_attempt:
            return True
    return False


# LLM: _float_attr keeps timestamp comparisons tolerant of old task/grant records.
# 函数用途: 读取旧记录中可能为空或字符串的时间戳；坏值按 0 处理，不让调度崩溃。
def _float_attr(value: object, name: str) -> float:
    try:
        return float(getattr(value, name, 0.0) or 0.0)
    except (TypeError, ValueError):
        return 0.0


# LLM: _runner_active_attempt_id keeps active-runner reentry checks concrete and MagicMock-safe.
# 函数用途: 读取任务当前 active attempt；存在时说明该 run 已有执行器在跑，普通 dispatch 不应再次启动。
def _runner_active_attempt_id(task: SubAgentTask) -> str:
    value = getattr(task, "runner_active_attempt_id", "")
    if not isinstance(value, str):
        return ""
    return value.strip()
