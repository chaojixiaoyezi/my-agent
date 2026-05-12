# LLM: Subagent orchestration module; keep task workspace, manager facade, and report contracts stable.
# 模块用途: 支撑主代理派发、跟踪、验收、汇总子代理任务。

from __future__ import annotations

"""task state mutation rules for runner result recording.

给人看的解释：
runner 写回状态的分支比较多，单独放这里，manager mixin 只负责串起读写流程。
"""

from dataclasses import dataclass

from .capability_status import is_pending_capability_status
from .policies import _status_from_structured_output, _verification_from_runner_status

_RUNNER_FAILURE_STATUSES = {"BLOCKED", "FAILED", "CHANNEL_ERROR", "TIMEOUT"}


# LLM: RunnerResultFieldParams 属于子代理任务管理的类边界；调整时先确认任务状态、执行器结果、验收和报告展示仍按原契约工作。
# 类用途: 集中保存执行器结果字段参数字段，让调用方按同一参数包传递上下文；关键副作用: 本身不执行输入输出；字段变化会影响构造点、序列化和测试读取。
@dataclass(frozen=True)
class RunnerResultFieldParams:

    task: object
    result_meta: dict
    status_context: dict
    parsed: object
    now: float


# LLM: RunnerAttemptParams 属于子代理任务管理的类边界；调整时先确认任务状态、执行器结果、验收和报告展示仍按原契约工作。
# 类用途: 集中保存执行器attempt参数字段，让调用方按同一参数包传递上下文；关键副作用: 本身不执行输入输出；字段变化会影响构造点、序列化和测试读取。
@dataclass(frozen=True)
class RunnerAttemptParams:

    task: object
    dry_run: bool
    ok: bool
    message: str
    now: float


# LLM: apply_runner_result_fields 属于子代理任务管理的函数边界；调整时先确认任务状态、执行器结果、验收和报告展示仍按原契约工作。
# 函数用途: 更新执行器结果字段对应的任务或运行状态，并保留既有字段语义；关键副作用: 会影响任务状态、执行器结果、验收和报告展示，需保持重试、超时和状态迁移语义。
def apply_runner_result_fields(params: RunnerResultFieldParams) -> None:
    """Apply parsed runner status and raw fallback status to a task in place."""
    task = params.task
    result_meta = params.result_meta
    status_context = params.status_context
    parsed = params.parsed
    ok = result_meta["ok"]
    message = result_meta["message"]
    response = result_meta["response"]
    dry_run = result_meta["dry_run"]
    parsed_ok = parsed.ok
    _apply_status_fields(task, status_context, parsed)

    if parsed.found and not parsed_ok:
        ok = False
        message = f"{message} / structured output parse failed: {parsed.parse_error}"
        task.result = response or message
    elif not parsed.found:
        _apply_unstructured_failure(task, ok, status_context["failure_type"])
        task.result = response or message or task.result

    if response and not (parsed.found and not parsed_ok):
        task.result = response
    elif message:
        task.result = message
    _apply_runner_timestamps(task, params.now)
    _apply_runner_attempt_fields(RunnerAttemptParams(task, dry_run, ok, message, params.now))
    result_meta["ok"] = ok
    result_meta["message"] = message


# LLM: _apply_status_fields 属于子代理任务管理的函数边界；调整时先确认任务状态、执行器结果、验收和报告展示仍按原契约工作。
# 函数用途: 更新状态字段对应的任务或运行状态，并保留既有字段语义；关键副作用: 会更新任务状态、执行器结果、验收和报告展示，需避免破坏既有状态机约定。
def _apply_status_fields(task, status_context, parsed) -> None:
    status = status_context["status"]
    verification_status = status_context["verification_status"]
    failure_type = status_context["failure_type"]
    if parsed.found and parsed.ok:
        task.status = (status or _status_from_structured_output(parsed)).upper()
        task.verification_status = (verification_status or _verification_from_runner_status(task.status)).upper()
        _apply_structured_failure_state(task, failure_type or parsed.failure_type, parsed)
        return
    if parsed.found and not parsed.ok:
        task.status = status.upper() if status else "BLOCKED"
        task.verification_status = verification_status.upper() if verification_status else "UNVERIFIED"
        if _has_open_capability_requests(task):
            task.failure_type = failure_type or "capability_request"
            _append_open_request_blocker(task)
        else:
            task.failure_type = failure_type or "structured_output_parse_error"
        return
    if status:
        task.status = status.upper()
    if verification_status:
        task.verification_status = verification_status.upper()
    if failure_type:
        task.failure_type = failure_type


# LLM: _apply_structured_failure_state keeps stale recovery state from surviving a successful retry.
# 函数用途: 根据当前结构化输出刷新 failure_type/blockers/open capability request，成功结果会清掉旧阻塞态。
def _apply_structured_failure_state(task, current_failure_type: str, parsed) -> None:
    if current_failure_type:
        task.failure_type = current_failure_type
        return
    if is_pending_capability_status(str(getattr(parsed, "status", "") or "")):
        task.failure_type = "capability_request"
        return
    if parsed.capability_requests or parsed.blocked_reason:
        task.failure_type = "capability_request"
        return
    if _should_resolve_stale_capability_requests(task):
        _resolve_stale_capability_requests(task)
    if _has_open_capability_requests(task):
        task.status = "BLOCKED"
        task.verification_status = "UNVERIFIED"
        task.failure_type = "capability_request"
        _append_open_request_blocker(task)
        return
    if task.status in _RUNNER_FAILURE_STATUSES:
        task.failure_type = task.failure_type or task.status.lower()
        return
    task.failure_type = ""
    task.blockers = []
    _resolve_stale_capability_requests(task)


# LLM: _should_resolve_stale_capability_requests allows successful retries to clear their old blocker.
# 函数用途: 只有旧状态本来就是 capability_request 阻塞时，才把 OPEN 请求视为 stale 并解除。
def _should_resolve_stale_capability_requests(task) -> bool:
    return str(getattr(task, "failure_type", "") or "") == "capability_request"


# LLM: _has_open_capability_requests protects tool-created requests from accidental success cleanup.
# 函数用途: 判断任务是否已有 OPEN 能力申请；这种情况下 runner 不能进入等待验收或完成态。
def _has_open_capability_requests(task) -> bool:
    return any(getattr(request, "status", "") == "OPEN" for request in getattr(task, "capability_requests", []) or [])


# LLM: _append_open_request_blocker gives parent recovery a stable reason without duplicating blockers.
# 函数用途: 给已有 OPEN 能力申请补一条 blocker，避免看板只看到 BLOCKED 但不知道下一步。
def _append_open_request_blocker(task) -> None:
    blocker = "已有 OPEN capability_request，等待父级 route_capability_request。"
    if blocker not in getattr(task, "blockers", []):
        task.blockers.append(blocker)


# LLM: _resolve_stale_capability_requests marks old OPEN requests inactive once the runner has current evidence.
# 函数用途: 成功重跑且当前输出不再请求能力时，把旧 OPEN 能力请求标为 RESOLVED，避免看板继续报假阻塞。
def _resolve_stale_capability_requests(task) -> None:
    for request in getattr(task, "capability_requests", []) or []:
        if getattr(request, "status", "") == "OPEN":
            request.status = "RESOLVED"


# LLM: _apply_unstructured_failure 属于子代理任务管理的函数边界；调整时先确认任务状态、执行器结果、验收和报告展示仍按原契约工作。
# 函数用途: 更新unstructured失败对应的任务或运行状态，并保留既有字段语义；关键副作用: 会更新任务状态、执行器结果、验收和报告展示，需避免破坏既有状态机约定。
def _apply_unstructured_failure(task, ok, failure_type: str) -> None:
    if failure_type:
        task.failure_type = failure_type
    elif not ok:
        task.failure_type = task.failure_type or "runner_error"


# LLM: _apply_runner_timestamps 属于子代理任务管理的函数边界；调整时先确认任务状态、执行器结果、验收和报告展示仍按原契约工作。
# 函数用途: 更新执行器timestamps对应的任务或运行状态，并保留既有字段语义；关键副作用: 会影响任务状态、执行器结果、验收和报告展示，需保持重试、超时和状态迁移语义。
def _apply_runner_timestamps(task, now: float) -> None:
    if task.status in {"DONE", "FAILED", "BLOCKED", "CHANNEL_ERROR", "TIMEOUT"}:
        task.ended_at = now
    task.updated_at = now
    task.heartbeat_at = now


# LLM: _apply_runner_attempt_fields 属于子代理任务管理的函数边界；调整时先确认任务状态、执行器结果、验收和报告展示仍按原契约工作。
# 函数用途: 更新执行器attempt字段对应的任务或运行状态，并保留既有字段语义；关键副作用: 会影响任务状态、执行器结果、验收和报告展示，需保持重试、超时和状态迁移语义。
def _apply_runner_attempt_fields(params: RunnerAttemptParams) -> None:
    task = params.task
    if params.dry_run:
        return
    task.runner_attempts = max(0, int(task.runner_attempts or 0)) + 1
    task.runner_last_attempt_at = params.now
    if not params.ok or task.status in {"BLOCKED", "FAILED", "CHANNEL_ERROR", "TIMEOUT"}:
        task.runner_last_error = params.message
    else:
        task.runner_last_error = ""
    if str(task.runner_active_attempt_id or "").strip():
        task.runner_active_attempt_id = ""
