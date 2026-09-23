# LLM: 本模块只裁决 runner 结果能否写回当前 exact run／attempt；拒绝时保留原快速回执与结构化诊断。
# 模块用途: 拦住被接管、已废弃、已换代或与运行账终态冲突的迟到结果，不触碰结果文件和父级通知。
from __future__ import annotations

import time
from typing import Any

from ..runtime_db.operations import AGENT_RUN_TERMINAL_STATUSES, RUN_STATUS_LEGACY_CREATED
from ..turn_end import subagent_outcome_for_turn_end
from .manager_runner_result_payload import RecordRunnerResultParams
from .models import SubAgentRunnerResult, SubAgentTask, TaskStatus


# LLM: 准入只读取当前 canonical task 与 RuntimeDB；被拒结果不再进入文件、WAL 或通知链。
#   终态冲突仍向原运行账追加诊断，诊断故障不能反过来放行结果。
# 函数用途: 在正式写结果前拦住旧轮、接管任务和运行时终态冲突。
def reject_stale_runner_result(
    manager: Any,
    task: SubAgentTask,
    params: RecordRunnerResultParams,
) -> SubAgentRunnerResult | None:
    attempt_id = params.attempt_id
    dry_run = params.dry_run
    if not dry_run and (_task_text_attr(task, "status").upper() == "TAKEN_OVER" or _task_text_attr(task, "takeover_by")):
        return make_rejected_runner_result(task, dry_run, False, "ignored runner result for already taken-over run")
    normalized_attempt_id = str(attempt_id or "").strip()
    if normalized_attempt_id:
        if normalized_attempt_id in _task_list_attr(task, "runner_abandoned_attempt_ids"):
            return make_rejected_runner_result(task, dry_run, False, f"ignored stale runner result for abandoned attempt {normalized_attempt_id}")
        active_attempt_id = _task_text_attr(task, "runner_active_attempt_id")
        if not active_attempt_id:
            return make_rejected_runner_result(task, dry_run, False, f"ignored duplicate runner result for inactive attempt {normalized_attempt_id}")
        if active_attempt_id and active_attempt_id != normalized_attempt_id:
            return make_rejected_runner_result(task, dry_run, False, f"ignored stale runner result for non-active attempt {normalized_attempt_id}")
        conflict = _managed_runtime_result_conflict(manager, task, params)
        if conflict:
            _record_conflict_diagnostic(manager, task, params, conflict=conflict)
            return make_rejected_runner_result(task, dry_run, False, conflict)
    return None


# LLM: 冲突诊断必须写到 manager 持有的原 RuntimeDB；服务对象自己没有 runtime_db。
#   追加事件 fail-soft，不改变已拒绝的准入裁决或尝试编号。
# 函数用途: 记录运行时终态闸拒绝的精确 run／attempt，供恢复排障和监督。
def _record_conflict_diagnostic(
    manager: Any,
    task: SubAgentTask,
    params: RecordRunnerResultParams,
    *,
    conflict: str,
) -> None:
    repo = getattr(manager, "runtime_db", None)
    append = getattr(repo, "append_event", None)
    if not callable(append):
        return
    try:
        authority = repo.runner_result_commit_authority(
            run_id=str(task.id or ""),
            attempt_id=str(params.attempt_id or ""),
        ) or {}
    except Exception:
        authority = {}
    try:
        append(
            event_type="closeout_blocked",
            attempt_id=str(params.attempt_id or ""),
            agent_run_id=str((authority or {}).get("agent_run_id") or ""),
            task_run_id=str((authority or {}).get("task_run_id") or ""),
            payload={
                "schema_version": "subagent-closeout-conflict.v1",
                "reason": "runner_result_conflict",
                "conflict": str(conflict),
                "run_status": str((authority or {}).get("run_status") or ""),
                "attempt_status": str((authority or {}).get("attempt_status") or ""),
                "incoming_status": str(getattr(params, "status", "") or ""),
                "incoming_turn_end_reason": str(getattr(params, "turn_end_reason", "") or ""),
                "task_status": str(_task_text_attr(task, "status") or ""),
            },
        )
    except Exception:
        return


# LLM: 拒绝回执只保留原 task 投影和错误原因，不能改任务或伪造终态。
# 函数用途: 给无法写回的 runner 结果生成与原入口相同的快速回执。
def make_rejected_runner_result(
    task: SubAgentTask,
    dry_run: bool,
    ok: bool,
    message: str,
) -> SubAgentRunnerResult:
    return SubAgentRunnerResult(
        run_id=task.id, dry_run=dry_run, ok=ok, status=task.status,
        verification_status=task.verification_status, message=message,
        turn_end_reason=str(getattr(task, "turn_end_reason", "") or ""),
        runner_attempts=task.runner_attempts, runner_last_error=task.runner_last_error,
        execution_context_json=task.execution_context_json, execution_context_file=task.execution_context_file,
        prompt_file=task.runner_prompt_file, response_file=task.runner_response_file,
        result_file=task.runner_result_file, result_json=task.runner_result_json,
        output_json=task.output_json, created_at=time.time(),
    )


# LLM: RuntimeDB 的终态与当前 attempt 是准入权威；未知脏状态保留 runner 结论给 WAL 恢复，
#   一致终态重入放行，冲突和换代拒绝，不能据模型正文补猜。
# 函数用途: 核对当前运行账是否允许这条 runner 结果进入正式写回。
def _managed_runtime_result_conflict(
    manager: Any,
    task: SubAgentTask,
    params: RecordRunnerResultParams,
) -> str:
    repo = getattr(manager, "runtime_db", None)
    if repo is None or params.dry_run:
        return ""
    authority = repo.runner_result_commit_authority(
        run_id=str(task.id or ""),
        attempt_id=str(params.attempt_id or ""),
    )
    if authority is None:
        return f"ignored runner result without runtime authority for attempt {params.attempt_id}"
    if not bool(authority.get("is_current")):
        return f"ignored stale runner result for superseded attempt {params.attempt_id}"
    run_status = str(authority.get("run_status") or "")
    attempt_status = str(authority.get("attempt_status") or "")
    if run_status in {"", "created"} and attempt_status == "running":
        return ""
    if run_status not in RUN_STATUS_LEGACY_CREATED and run_status not in AGENT_RUN_TERMINAL_STATUSES:
        # 未知状态不是权威终态：结论照常落账，由 closeout 记 unknown_status 等待显式恢复。
        return ""
    incoming_terminal = _runner_runtime_terminal_status(params)
    if _runner_result_matches_settled_attempt(run_status, attempt_status, params):
        return ""
    if run_status in AGENT_RUN_TERMINAL_STATUSES:
        # 同一 exact current attempt 的一致终态可重入，补齐收口后尚未交付的父级通知。
        if incoming_terminal and incoming_terminal == run_status:
            return ""
        if not incoming_terminal and run_status == "done" and attempt_status == "done":
            return ""
    return (
        "ignored runner result conflicting with runtime terminal fact "
        f"run={run_status or 'unknown'} attempt={attempt_status or 'unknown'} "
        f"incoming={incoming_terminal or 'nonterminal'}"
    )


# LLM: 任务字段只能做当前准入辅助，不能替代 RuntimeDB 的 exact attempt 权威。
# 函数用途: 从任务对象安全读取一个文本字段，供旧轮和接管判断。
def _task_text_attr(task: SubAgentTask, name: str) -> str:
    value = getattr(task, name, "")
    return value.strip() if isinstance(value, str) else ""


# LLM: 已废弃 attempt 集合只读取 canonical task；非集合字段视作没有可用条目。
# 函数用途: 安全读取旧轮编号集合，避免误让已放弃结果进入写回。
def _task_list_attr(task: SubAgentTask, name: str) -> list[str]:
    value = getattr(task, name, [])
    if not isinstance(value, list | tuple | set):
        return []
    return [str(item) for item in value if str(item or "").strip()]


# LLM: 只从结构化 runner 状态映射 runtime 终态，不读取展示标签或模型回复。
# 函数用途: 把本轮 runner 结论转为运行账使用的三种终态。
def _runner_runtime_terminal_status(params: RecordRunnerResultParams) -> str:
    raw_status = str(params.status or "").strip().upper()
    if not raw_status and params.structured_output is not None:
        raw_status = str(getattr(params.structured_output, "status", "") or "").strip().upper()
    if raw_status == TaskStatus.DONE.value:
        return "done"
    if raw_status in {
        TaskStatus.CANCELLED.value,
        TaskStatus.ABANDONED.value,
        TaskStatus.TAKEN_OVER.value,
    }:
        return "cancelled"
    if raw_status in {
        TaskStatus.FAILED.value,
        TaskStatus.TIMEOUT.value,
        TaskStatus.CHANNEL_ERROR.value,
    }:
        return "failed"
    return ""


# LLM: 已结清的当前 attempt 只接受宿主 typed turn_end 对应的确切状态；前置调用方已核对
#   authority.is_current，正文和结构化输出不能在结清后自行取得写回权。
# 函数用途: 判断 run 仍 created、attempt 已 done 时，同轮结论是否可续跑或应正式收口。
def _runner_result_matches_settled_attempt(
    run_status: str,
    attempt_status: str,
    params: RecordRunnerResultParams,
) -> bool:
    if run_status not in {"", "created"} or attempt_status != "done":
        return False
    raw_status = str(params.status or "").strip().upper()
    expected_status, _failure_type, _ok = subagent_outcome_for_turn_end(params.turn_end_reason)
    if expected_status in {
        TaskStatus.PENDING.value,
        TaskStatus.BLOCKED.value,
    }:
        return raw_status == expected_status
    if expected_status in {
        TaskStatus.FAILED.value,
        TaskStatus.CANCELLED.value,
    }:
        return raw_status == expected_status
    return False
