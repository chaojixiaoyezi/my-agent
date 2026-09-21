# LLM: runner 写回遵守唯一 turn_end 协议；文件消费身份随原结果回收，未完成不等于失败，联测持久结果、直属等待和恢复。
# 模块用途: 把子代理本轮状态、失败分类和活动信息写回任务对象，由调用方沿原账本持久化。
from __future__ import annotations

"""task state mutation rules for runner result recording.

runner 写回状态的分支比较多，单独放这里，manager mixin 只负责串起读写流程。
显式 runner status 只接受当前 TaskStatus 协议值；未知原文在这里直接失败关闭。
"""

from dataclasses import dataclass
from pathlib import Path

from ..turn_end import normalize_turn_end_reason, subagent_outcome_for_turn_end
from .model_capabilities import (
    capability_request_counts_as_open,
    capability_request_requires_parent_resolution,
    is_pending_capability_status,
)
from .models import (
    SUBAGENT_FAILURE_STATUSES,
    FailureType,
    TaskStatus,
    VerificationStatus,
    failure_type_from_task_status,
    known_failure_type,
    normalize_task_status,
    normalize_verification_status,
    task_has_ended_status,
    task_has_failure_status,
    task_has_status,
    task_status_in,
    task_status_reason_code,
)
from .policies import _status_from_structured_output, _verification_from_runner_status
from .service_window import service_window_remaining_seconds

_RUNNER_FAILURE_STATUSES = SUBAGENT_FAILURE_STATUSES


@dataclass(frozen=True)
class RunnerResultFieldParams:

    task: object
    result_meta: dict
    status_context: dict
    parsed: object
    now: float


# LLM: 显式结束原因只描述本轮宿主事实，不读取旧 task.turn_end_reason；调用方保持原 attempt 计数与清理顺序。
# 类用途: 携带当前尝试的记账字段，区分正常未完成与真正错误，不新增持久格式。
@dataclass(frozen=True)
class RunnerAttemptParams:

    task: object
    dry_run: bool
    ok: bool
    message: str
    now: float
    turn_end_reason: str


# LLM: 宿主授权事实优先；显式结束原因与状态共同分类失败，不能把未完成当异常；保持租约释放和 attempt 顺序。
# 函数用途: 更新 runner 任务、尝试计数与当前活动，正常让出保留可恢复状态并清除旧错误投影。
def apply_runner_result_fields(params: RunnerResultFieldParams) -> None:
    """Apply parsed runner status and raw status text to a task in place."""
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
    stale_binding_transition_response = _consume_source_binding_transition_response(
        task
    )

    ok, message = _runner_result_outcome(task, parsed, result_meta, status_context)

    if _source_worker_closed_by_ledger(task):
        # A named clear is the durable lifecycle authority.  Keep generated
        # late output in the runner archive, but do not project it back onto
        # the cancelled task as its current result.
        task.result = message
    elif stale_binding_transition_response:
        # The raw response remains in the immutable runner archive, but it was
        # generated from the pre-binding snapshot and must not become the
        # coordinator-visible current summary.
        task.result = ""
    elif response and not (parsed.found and not parsed_ok):
        task.result = response
    elif message:
        task.result = message
    _apply_runner_timestamps(task, params.now)
    _apply_runner_attempt_fields(RunnerAttemptParams(
        task, dry_run, ok, message, params.now, status_context.get("turn_end_reason", ""),
    ))
    apply_runner_display_status(task)
    _release_source_worker_lease_after_result(task, dry_run=dry_run)
    _reclaim_settled_runner_launch(task)
    if ok and not dry_run:
        from ..ingestion.source_worker import record_source_worker_progress

        record_source_worker_progress(task, now=params.now)
    result_meta["ok"] = ok
    result_meta["message"] = message


# LLM: Display text is a projection of typed status/failure facts only. It may
# never copy model prose into a terminal activity label or affect lifecycle.
# 函数用途: 把子代理结束、等待或失败状态投影成 TUI 可读的当前步骤。
def apply_runner_display_status(task: object) -> None:
    """Project typed lifecycle facts into a short display-only activity label."""
    status = str(getattr(task, "status", "") or "").strip().upper()
    if status == TaskStatus.RUNNING.value:
        return
    failure_type = str(getattr(task, "failure_type", "") or "").strip()
    detail_by_failure = {
        FailureType.CAPABILITY_REQUEST.value: "等待父级授权",
        FailureType.PERMISSION_BLOCKED.value: "等待授权",
        FailureType.WRITE_PERMISSION_BLOCKED.value: "等待授权",
        FailureType.PROVIDER_QUOTA_EXHAUSTED.value: "额度不足",
        FailureType.STRUCTURED_OUTPUT_PARSE_ERROR.value: "结果格式异常",
    }
    label = detail_by_failure.get(failure_type, "")
    if not label:
        label = {
            TaskStatus.DONE.value: "已完成",
            TaskStatus.FAILED.value: "失败",
            TaskStatus.TIMEOUT.value: "超时",
            TaskStatus.CHANNEL_ERROR.value: "连接失败",
            TaskStatus.CANCELLED.value: "已停止",
            TaskStatus.ABANDONED.value: "已停止",
            TaskStatus.TAKEN_OVER.value: "已接管",
            TaskStatus.BLOCKED.value: "等待处理",
            TaskStatus.PAUSED.value: "已暂停",
            TaskStatus.PENDING.value: "等待继续",
            TaskStatus.PLANNING.value: "等待继续",
        }.get(status, status or "")
    if not label:
        return
    task.current_step = label
    task.current_tool = ""


def _consume_source_binding_transition_response(task: object) -> bool:
    """Discard prose only from the exact attempt that crossed source binding."""

    from ..common.audit_activation import (
        AUDIT_SOURCE_CONTEXT_REFRESH_ATTEMPT_ATTR,
    )

    attrs = dict(getattr(task, "attributes", {}) or {})
    expected_attempt = str(
        attrs.pop(AUDIT_SOURCE_CONTEXT_REFRESH_ATTEMPT_ATTR, "") or ""
    ).strip()
    if not expected_attempt:
        return False
    task.attributes = attrs
    current_attempt = str(
        getattr(task, "runner_active_attempt_id", "") or ""
    ).strip()
    # A crash after binding can leave the marker for a later recovery attempt.
    # That later attempt has a different id and its fresh result remains valid.
    return bool(current_attempt and current_attempt == expected_attempt)


# LLM: 先沿宿主来源/授权状态裁决；未解析正文的失败分类只读显式结束原因，不能用 ok=False 判断所有未完成均失败。
# 函数用途: 更新当前结果及失败字段并返回原成功标志和说明；正常让出仍未完成，不冒充成功收口。
def _runner_result_outcome(task, parsed, result_meta: dict, status_context: dict) -> tuple[bool, str]:
    ok = result_meta["ok"]
    message = result_meta["message"]
    response = result_meta["response"]
    if _source_worker_closed_by_ledger(task):
        return False, "Audit source was closed; late runner result ignored"
    if _source_worker_completed_by_ledger(task):
        # LLM: The durable watch ledger, not a model-authored capability request
        # or result block, proves that a source worker finished its bounded job.
        # 中文说明：来源窗口结束且欠账清零后，持久账本就是完成事实；模型误申请写报告
        # 或结果块格式有瑕疵，都不能把已经清账的来源岗位伪装成 BLOCKED。
        return True, "Audit source window complete and backlog settled"
    if _has_open_capability_requests(task) and not _structured_capability_recovery_result(
        task,
        parsed,
        status_context,
    ):
        return False, "runner 已提交 capability_request，等待直接父级裁决"
    if _awaiting_granted_capability_continuation(task, status_context):
        return True, "capability_request 已授权，同一 run 等待自动续跑"
    if parsed.found and not parsed.ok:
        message = f"{message} / structured output parse failed: {parsed.parse_error}"
        task.result = response or message
        return False, message
    if parsed.found and task_has_failure_status(task):
        return False, parsed.blocked_reason or message or task.failure_type or task_status_reason_code(task.status)
    if not parsed.found:
        _apply_unstructured_failure(task, ok, status_context)
        task.result = response or message or task.result
    return ok, message


# LLM: OPEN requests and same-attempt grants are typed lifecycle facts and
# therefore outrank parsed/unparsed completion. Audit source ledgers remain the
# more specific authority and are handled first.
# 函数用途: 按宿主事实把子代理轮次投影成可恢复的任务状态。
def _apply_status_fields(task, status_context, parsed) -> None:
    status = status_context["status"]
    verification_status = status_context["verification_status"]
    failure_type = known_failure_type(status_context["failure_type"])
    parsed_failure_type = known_failure_type(getattr(parsed, "failure_type", ""))
    if _source_worker_closed_by_ledger(task):
        # Named Audit clear closes the persisted watch before cancelling its
        # descendants.  A runner may finish in that small interval; the model
        # result must not revive or block a source whose lifecycle is already
        # closed.
        task.status = TaskStatus.CANCELLED.value
        task.verification_status = VerificationStatus.UNVERIFIED.value
        task.failure_type = FailureType.CANCELLED.value
        task.blockers = []
        _resolve_stale_capability_requests(task)
        return
    if _source_worker_still_has_work(task):
        # LLM: 持久来源账本只决定“还要不要续派”，不是质量验收。
        # 函数用途: 来源窗口仍开放时保持同一 run 可续派，不被单轮自然回复提前关闭。
        task.status = TaskStatus.PENDING.value
        task.verification_status = VerificationStatus.UNVERIFIED.value
        task.failure_type = ""
        task.blockers = []
        task.ended_at = 0.0
        return
    if _source_worker_completed_by_ledger(task):
        # LLM: Source workers deliver verdicts/findings into durable ledgers;
        # they never need a second report-file closeout path.
        # 中文说明：来源子代理的交付物就是已持久化的 verdict/finding；账本清零后直接
        # 进入 DONE，并关闭模型误报的能力申请，不再等待不存在的报告文件。
        task.status = TaskStatus.DONE.value
        task.verification_status = VerificationStatus.VERIFIED.value
        task.failure_type = ""
        task.blockers = []
        _resolve_stale_capability_requests(task)
        return
    if _has_open_capability_requests(task) and not _structured_capability_recovery_result(
        task,
        parsed,
        status_context,
    ):
        task.status = TaskStatus.BLOCKED.value
        task.verification_status = VerificationStatus.UNVERIFIED.value
        task.failure_type = FailureType.CAPABILITY_REQUEST.value
        _append_open_request_blocker(task)
        return
    if _awaiting_granted_capability_continuation(task, status_context):
        task.status = TaskStatus.PENDING.value
        task.verification_status = VerificationStatus.UNVERIFIED.value
        task.failure_type = FailureType.CAPABILITY_REQUEST.value
        task.blockers = []
        task.ended_at = 0.0
        return
    # Explicit runner statuses fail closed instead of translating unknown raw text.
    if parsed.found and parsed.ok:
        task.status = normalize_task_status(status or _status_from_structured_output(parsed))
        task.verification_status = _normalized_verification_status(
            verification_status or _verification_from_runner_status(task.status)
        )
        _apply_structured_failure_state(task, failure_type or parsed_failure_type, parsed)
        return
    if parsed.found and not parsed.ok:
        # A malformed final result block is still recorded as an attempt
        # failure.  For a durable Audit source worker, however, it must not
        # convert an open watch with remaining ledger work into a permanent
        # BLOCKED state.  Keeping the task PENDING lets the canonical source
        # lifecycle resume the same worker identity on the next dispatch,
        # while ``result_meta["ok"]`` remains false and the parse failure stays
        # visible in runner history.  Typed provider/tool/permission failures
        # do not enter this branch and retain their ordinary failure semantics.
        if _source_worker_still_has_work(task) and not _has_open_capability_requests(task):
            task.status = TaskStatus.PENDING.value
            task.verification_status = VerificationStatus.UNVERIFIED.value
            task.failure_type = FailureType.STRUCTURED_OUTPUT_PARSE_ERROR.value
            task.blockers = []
            task.ended_at = 0.0
            return
        task.status = normalize_task_status(status) if status else TaskStatus.BLOCKED.value
        task.verification_status = (
            normalize_verification_status(verification_status) if verification_status else VerificationStatus.UNVERIFIED.value
        )
        if _has_open_capability_requests(task):
            task.failure_type = failure_type or parsed_failure_type or FailureType.CAPABILITY_REQUEST.value
            _append_open_request_blocker(task)
        else:
            task.failure_type = failure_type or parsed_failure_type or FailureType.STRUCTURED_OUTPUT_PARSE_ERROR.value
        return
    if status:
        task.status = normalize_task_status(status)
    if verification_status:
        task.verification_status = normalize_verification_status(verification_status)
    if failure_type:
        task.failure_type = failure_type


def _apply_structured_failure_state(task, current_failure_type: str, parsed) -> None:
    if _source_worker_still_has_work(task):
        # LLM: A model may end one source-worker turn as DONE, PENDING or
        # BLOCKED, but the durable watch ledger is the lifecycle authority.
        # 中文说明：来源工作者一轮结束时无论模型写 DONE、PENDING 还是 BLOCKED，
        # 只要持久欠账未清，就必须沿同一逻辑岗位续跑，不能被模型状态提前关停。
        task.status = TaskStatus.PENDING.value
        task.verification_status = VerificationStatus.UNVERIFIED.value
        task.failure_type = FailureType.INCOMPLETE_DELIVERABLES.value
        task.blockers = []
        task.ended_at = 0.0
        _resolve_stale_capability_requests(task)
        return
    if task_has_status(task, TaskStatus.DONE) and service_window_remaining_seconds(task) > 0:
        # LLM: ``long_running + service_window_seconds`` is a typed lifecycle
        # contract.  A model-authored DONE closes one bounded turn, not the
        # declared service window.  Keep the same durable run dispatchable;
        # no goal-text matching or Audit-specific prompt exception is used.
        # 中文说明：模型写 DONE 只能结束这一轮；结构化值守窗口尚未结束时，同一
        # run 必须继续可派发，不能靠模型一句“完成”提前关停持续任务。
        task.status = TaskStatus.PENDING.value
        task.verification_status = VerificationStatus.UNVERIFIED.value
        task.failure_type = FailureType.INCOMPLETE_DELIVERABLES.value
        task.blockers = []
        task.ended_at = 0.0
        _resolve_stale_capability_requests(task)
        return
    if _is_recoverable_incomplete_result(task, current_failure_type, parsed):
        # A model turn ending before its declared artifacts are ready is not a
        # hard blocker.  Keep the same run dispatchable so its durable
        # checkpoint/tool archive can continue; do not force the parent to
        # create a replacement child and redo work.
        task.status = TaskStatus.PENDING.value
        task.verification_status = VerificationStatus.UNVERIFIED.value
        task.failure_type = FailureType.INCOMPLETE_DELIVERABLES.value
        task.blockers = []
        return
    if current_failure_type:
        task.failure_type = current_failure_type
        return
    if is_pending_capability_status(str(getattr(parsed, "status", "") or "")):
        task.failure_type = FailureType.CAPABILITY_REQUEST.value
        return
    if parsed.capability_requests:
        task.failure_type = FailureType.CAPABILITY_REQUEST.value
        return
    if _should_resolve_stale_capability_requests(task):
        _resolve_stale_capability_requests(task)
    if _has_open_capability_requests(task):
        task.status = TaskStatus.BLOCKED.value
        task.verification_status = VerificationStatus.UNVERIFIED.value
        task.failure_type = FailureType.CAPABILITY_REQUEST.value
        _append_open_request_blocker(task)
        return
    if _blocked_with_attempt_fresh_grant(task):
        # The requested capability is already present as a typed grant, so this
        # attempt has no unresolved external blocker.  Requeue the same run and
        # let the post-session durable starter continue it; keeping BLOCKED would
        # also close its conversation link and make the dispatch gate hold it.
        task.status = TaskStatus.PENDING.value
        task.verification_status = VerificationStatus.UNVERIFIED.value
        task.failure_type = FailureType.CAPABILITY_REQUEST.value
        task.blockers = []
        task.ended_at = 0.0
        return
    if task_has_failure_status(task):
        task.failure_type = task.failure_type or failure_type_from_task_status(task.status)
        return
    task.failure_type = ""
    task.blockers = []
    _resolve_stale_capability_requests(task)


# LLM: Only the source-worker adapter knows whether its persisted watch is
# still open or has unacknowledged records; runner prose is not consulted.
# 函数用途: 来源子代理尚有采集/待判工作时，把本轮 DONE 收口转换为同 run 可续跑状态。
def _source_worker_still_has_work(task: object) -> bool:
    attrs = getattr(task, "attributes", {}) or {}
    from ..common.audit_activation import (
        AUDIT_DEADLINE_ATTR,
        structured_audit_source_binding_attributes,
        structured_audit_source_worker_attributes,
    )

    if structured_audit_source_binding_attributes(attrs):
        try:
            deadline = float(attrs.get(AUDIT_DEADLINE_ATTR) or 0.0)
        except (TypeError, ValueError):
            deadline = 0.0
        if deadline <= 0:
            return True
        import time

        return time.time() < deadline
    if not structured_audit_source_worker_attributes(attrs):
        return False
    try:
        from ..ingestion.source_worker import source_worker_task_incomplete

        return source_worker_task_incomplete(task)
    except Exception:
        # A typed source worker with unreadable lifecycle facts fails closed
        # as still pending rather than accepting a generated terminal state.
        return True


# LLM: One background dispatch process may host several independent runners.
# Once any accepted result projects its exact run back to PENDING, that run's
# launch slot is over even if sibling threads keep the shared host PID alive.
# 文件模式没有 DB 终态；已消费身份在结果释放活动指针后同次回收，失败重试也领取新身份。
# Reclaim only the per-task record; never terminate or rewrite the shared host.
# 函数用途: 子代理让出或文件执行轮收口后释放自己的启动占位，不等待同批兄弟或重用已消费身份。
def _reclaim_settled_runner_launch(task: object) -> None:
    record = (task.attributes or {}).get("background_start") or {}
    settled_file_start = bool(record.get("activated_at") and not task.runner_active_attempt_id)
    if not task_has_status(task, TaskStatus.PENDING) and not settled_file_start:
        return
    from .process_control import reclaim_background_start

    reclaim_background_start(task)


# LLM: A source-worker lease belongs to one runner attempt, not to the durable
# logical worker.  Once an accepted runner result clears that attempt, release
# its lease in the same result projection so the next bounded slice does not
# wait for TTL expiry.  Stale results never reach this mutation path because
# the runner-result attempt guard rejects them first.
# 函数用途：来源工作者一轮收口时释放本 attempt 的临时消费租约，立即允许同一逻辑
# worker 续派；不删除 watch、游标、队列或判断账。
def _release_source_worker_lease_after_result(
    task: object,
    *,
    dry_run: bool,
) -> None:
    if dry_run:
        return
    attrs = getattr(task, "attributes", {}) or {}
    from ..common.audit_activation import (
        AUDIT_SOURCE_OWNER_HOME_ATTR,
        AUDIT_SOURCE_WATCH_ID_ATTR,
        structured_audit_source_worker_attributes,
    )

    if not structured_audit_source_worker_attributes(attrs):
        return
    owner_home = str(attrs.get(AUDIT_SOURCE_OWNER_HOME_ATTR) or "").strip()
    watch_id = str(attrs.get(AUDIT_SOURCE_WATCH_ID_ATTR) or "").strip()
    if not owner_home or not watch_id:
        return
    from ..ingestion.source_worker import clear_source_worker_lease

    clear_source_worker_lease(Path(owner_home), watch_id)


# LLM: A typed source worker may complete without producing a conventional
# file artifact. This helper deliberately returns False for ordinary agents and
# for unreadable source state, so only a proven settled ledger receives the
# specialized terminal projection.
# 函数用途：只在来源身份完整、窗口已结束且持久欠账确实为零时认定完成；普通子代理
# 和账本不可读场景继续走原有 fail-closed 收口。
def _source_worker_completed_by_ledger(task: object) -> bool:
    attrs = getattr(task, "attributes", {}) or {}
    from ..common.audit_activation import (
        structured_audit_source_worker_attributes,
    )

    if not structured_audit_source_worker_attributes(attrs):
        return False
    try:
        from ..ingestion.source_worker import source_worker_lifecycle_state

        return source_worker_lifecycle_state(task) == "complete"
    except Exception:
        return False


# LLM: A named Audit clear is a typed, durable cancellation fact.  It outranks
# every late model-authored runner status, just as the settled ledger outranks
# a generated completion claim.  Unreadable state remains fail-closed and does
# not invent cancellation.
# 函数用途：来源 watch 已被精确 clear 后，把迟到 runner 结果投影成 CANCELLED，
# 防止它把已关闭岗位重新写成 BLOCKED/PENDING/DONE。
def _source_worker_closed_by_ledger(task: object) -> bool:
    attrs = getattr(task, "attributes", {}) or {}
    from ..common.audit_activation import (
        structured_audit_source_worker_attributes,
    )

    if not structured_audit_source_worker_attributes(attrs):
        return False
    try:
        from ..ingestion.source_worker import source_worker_lifecycle_state

        return source_worker_lifecycle_state(task) == "closed"
    except Exception:
        return False


def _is_recoverable_incomplete_result(task, current_failure_type: str, parsed) -> bool:
    if current_failure_type != FailureType.INCOMPLETE_DELIVERABLES.value:
        return False
    if parsed.capability_requests or _has_open_capability_requests(task):
        return False
    return task_status_in(
        getattr(task, "status", ""),
        {TaskStatus.PENDING.value, TaskStatus.BLOCKED.value},
    )


def _should_resolve_stale_capability_requests(task) -> bool:
    return str(getattr(task, "failure_type", "") or "") == FailureType.CAPABILITY_REQUEST.value


# LLM: 常规能力申请在 runner 运行中被机制层自动批(capability_auto_grant)后,请求已
#   GRANTED 不再 open,BLOCKED 收尾会滑进 STATUS_BLOCKED——dispatch 的
#   _blocked_after_capability_grant 两个分支都不认(收尾还会把 runner_last_attempt_at
#   刷成结束时间,fresh-grant 判定也失效),授权后续跑断链。此处依赖调用顺序:
#   _apply_status_fields 先于 _apply_runner_attempt_fields,runner_last_attempt_at
#   此刻仍是本 attempt 的开始时间,"本次跑的过程中拿到新授权"是客观事实。续跑后没有
#   更新的 grant 就不再触发,天然一次性,不会无限续派。
# 函数用途: BLOCKED 收尾且本 attempt 内落过新 grant → 归为 CAPABILITY_REQUEST 让续派可达。
def _blocked_with_attempt_fresh_grant(task) -> bool:
    if not task_has_status(task, TaskStatus.BLOCKED):
        return False
    return _attempt_has_fresh_capability_grant(task)


# LLM: A grant newer than the current attempt start is a structured same-turn
# fact. It does not prove task completion; it only makes the same run eligible
# to continue after the requesting turn returns.
# 函数用途: 判断当前 runner 这一轮内是否刚获得了新授权。
def _attempt_has_fresh_capability_grant(task) -> bool:
    started = _timestamp(getattr(task, "runner_last_attempt_at", 0.0))
    if started <= 0:
        return False
    return any(
        _timestamp(getattr(grant, "created_at", 0.0)) > started
        for grant in getattr(task, "capability_grants", []) or []
    )


# LLM: The last executed tool is structured call-order evidence. Only when it
# is capability_request and a fresh grant exists do we requeue; a child that
# used later tools may still legitimately complete in the same turn.
# 函数用途: 判断子代理是否在申请工具后立即收口，需用新授权续跑。
def _awaiting_granted_capability_continuation(task, status_context: dict) -> bool:
    tools = status_context.get("actual_tools")
    if not isinstance(tools, list | tuple) or not tools:
        return False
    return str(tools[-1] or "").strip() == "capability_request" and _attempt_has_fresh_capability_grant(task)


# LLM: A later structured DONE from a task already blocked on capability is
# explicit recovery evidence; only that case may close stale OPEN requests.
# 函数用途: 区分“本轮刚申请就退出”与“授权后续跑已真正完成”。
def _structured_capability_recovery_result(task, parsed, status_context: dict) -> bool:
    if not _should_resolve_stale_capability_requests(task):
        return False
    if not bool(getattr(parsed, "found", False)) or not bool(getattr(parsed, "ok", False)):
        return False
    tools = status_context.get("actual_tools")
    if isinstance(tools, list | tuple) and tools:
        if str(tools[-1] or "").strip() == "capability_request":
            return False
    try:
        status = normalize_task_status(
            status_context.get("status") or _status_from_structured_output(parsed)
        )
    except ValueError:
        return False
    return status == TaskStatus.DONE.value


def _timestamp(value: object) -> float:
    try:
        return float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _has_open_capability_requests(task) -> bool:
    return any(
        capability_request_requires_parent_resolution(getattr(request, "status", "OPEN"))
        for request in getattr(task, "capability_requests", []) or []
    )


def _append_open_request_blocker(task) -> None:
    blocker = "已有 OPEN capability_request，等待父级 route_capability_request。"
    if blocker not in getattr(task, "blockers", []):
        task.blockers.append(blocker)


def _resolve_stale_capability_requests(task) -> None:
    for request in getattr(task, "capability_requests", []) or []:
        if capability_request_counts_as_open(getattr(request, "status", "OPEN")):
            request.status = "CLOSED"


def _normalized_verification_status(value: object) -> str:
    return normalize_verification_status(value)


# LLM: 复用唯一轮结束映射，只接受显式合法 reason 与当前 status/ok 一致；未知原因或冲突不能豁免失败记录。
# 函数用途: 只读判断当前宿主结果是否明确不带失败，供任务字段及尝试诊断共用。
def _runner_turn_has_no_failure(task: object, ok: bool, turn_end_reason: object) -> bool:
    reason = normalize_turn_end_reason(turn_end_reason)
    if not reason:
        return False
    status, failure_type, expected_ok = subagent_outcome_for_turn_end(reason)
    return not failure_type and ok == expected_ok and task_status_in(getattr(task, "status", ""), {status})


# LLM: 明确失败类型优先，正常完成/让出清当前错误投影；未知或冲突结果保留原失败规则，不改历史尝试档案。
# 函数用途: 在无结构化模型结果时更新任务失败字段，防止通用 runner_error 污染正常等待及后续完成。
def _apply_unstructured_failure(task, ok, status_context: dict) -> None:
    known = known_failure_type(status_context["failure_type"])
    if known:
        task.failure_type = known
    elif _runner_turn_has_no_failure(task, ok, status_context.get("turn_end_reason", "")):
        task.failure_type = ""
    elif not ok:
        task.failure_type = task.failure_type or FailureType.RUNNER_ERROR.value


def _apply_runner_timestamps(task, now: float) -> None:
    if task_has_ended_status(task):
        task.ended_at = now
    if task_has_status(task, TaskStatus.DONE):
        task.progress = 1.0
    elif task_has_status(task, TaskStatus.RUNNING):
        task.progress = max(_safe_progress(getattr(task, "progress", 0.0)), 0.05)
    task.updated_at = now
    task.heartbeat_at = now


# LLM: 正常让出仍计一次真实 attempt 且保留 ok=False，但不写最近错误；typed 失败、未知原因和状态冲突仍留诊断。
# 函数用途: 更新尝试次数、时间和最近错误并释放当前 attempt 标识，不改变调度和持久化顺序。
def _apply_runner_attempt_fields(params: RunnerAttemptParams) -> None:
    task = params.task
    if params.dry_run:
        return
    task.runner_attempts = max(0, int(task.runner_attempts or 0)) + 1
    task.runner_last_attempt_at = params.now
    nonfailure = not task.failure_type and _runner_turn_has_no_failure(task, params.ok, params.turn_end_reason)
    if (not params.ok and not nonfailure) or task_has_failure_status(task):
        task.runner_last_error = params.message
    else:
        task.runner_last_error = ""
    if str(task.runner_active_attempt_id or "").strip():
        task.runner_active_attempt_id = ""


def _safe_progress(value: object) -> float:
    try:
        progress = float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0
    return min(1.0, max(0.0, progress))
