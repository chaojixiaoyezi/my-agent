from __future__ import annotations

from types import SimpleNamespace

from agent_py_agent.agent.agent_core.orchestration.dispatch.runner_candidates import (
    _runner_candidates_for_context,
)
from agent_py_agent.agent.agent_core.runner.dispatch import (
    RunnerCandidatePolicy,
    _is_dispatch_runner_candidate,
)
from agent_py_agent.agent.subagents.models import (
    FailureType,
    SubAgentParsedOutput,
    SubAgentTask,
    TaskStatus,
    VerificationStatus,
)
from agent_py_agent.agent.subagents.recovery_eligibility import (
    user_stopped_resume_eligibility,
    user_stopped_run_is_resumable,
)
from agent_py_agent.agent.subagents.runner_result_state import (
    RunnerResultFieldParams,
    apply_runner_result_fields,
)


def _task() -> SubAgentTask:
    return SubAgentTask(
        id="child-a",
        goal="继续完成既有产物",
        thought="沿原现场续跑",
        plan=["读取检查点", "完成产物"],
        status=TaskStatus.RUNNING.value,
        verification_status=VerificationStatus.UNVERIFIED.value,
    )


def _incomplete_result(*, status: str = "BLOCKED") -> SubAgentParsedOutput:
    return SubAgentParsedOutput(
        found=True,
        ok=True,
        status=status,
        summary="已经读取源码，但报告尚未写完。",
        blocked_reason="报告尚未写完。",
        failure_type=FailureType.INCOMPLETE_DELIVERABLES.value,
        next_actions=["沿原 run 写完报告"],
    )


def test_recoverable_incomplete_result_stays_pending_on_same_run() -> None:
    task = _task()
    result_meta = {
        "ok": True,
        "message": "model turn completed",
        "response": "structured result",
        "dry_run": False,
    }

    apply_runner_result_fields(
        RunnerResultFieldParams(
            task=task,
            result_meta=result_meta,
            status_context={"status": "", "verification_status": "", "failure_type": ""},
            parsed=_incomplete_result(),
            now=123.0,
        )
    )

    assert task.id == "child-a"
    assert task.status == TaskStatus.PENDING.value
    assert task.verification_status == VerificationStatus.UNVERIFIED.value
    assert task.failure_type == FailureType.INCOMPLETE_DELIVERABLES.value
    assert task.blockers == []
    assert task.runner_attempts == 1
    assert result_meta["ok"] is True
    assert _is_dispatch_runner_candidate(task) is True


def test_incomplete_result_with_open_capability_request_remains_blocked() -> None:
    task = _task()
    task.capability_requests = [SimpleNamespace(status="OPEN")]
    parsed = _incomplete_result()
    parsed.capability_requests = [{"problem": "need credential"}]
    result_meta = {
        "ok": True,
        "message": "model turn completed",
        "response": "structured result",
        "dry_run": False,
    }

    apply_runner_result_fields(
        RunnerResultFieldParams(
            task=task,
            result_meta=result_meta,
            status_context={"status": "", "verification_status": "", "failure_type": ""},
            parsed=parsed,
            now=123.0,
        )
    )

    assert task.status == TaskStatus.BLOCKED.value
    assert result_meta["ok"] is False


def test_persisted_incomplete_blocked_result_is_retryable_for_compatibility() -> None:
    task = SimpleNamespace(
        status=TaskStatus.BLOCKED.value,
        verification_status=VerificationStatus.UNVERIFIED.value,
        channel_status="OK",
        capability_requests=[],
        capability_gaps=[],
        capability_grants=[],
        failure_type=FailureType.INCOMPLETE_DELIVERABLES.value,
        runner_attempts=1,
    )

    assert _is_dispatch_runner_candidate(
        task,
        policy=RunnerCandidatePolicy(
            runner_max_attempts=2,
            same_run_redispatch_limit=1,
        ),
    ) is True


def test_conversation_user_stop_is_explicit_same_run_resume_candidate() -> None:
    task = _task()
    task.status = TaskStatus.CANCELLED.value
    task.failure_type = FailureType.CANCELLED.value
    task.attributes = {
        "cancel_subagents": {
            "reason": "conversation_user_stop",
            "previous_status": TaskStatus.BLOCKED.value,
            "previous_failure_type": FailureType.INCOMPLETE_DELIVERABLES.value,
        }
    }

    decision = user_stopped_resume_eligibility(task)

    assert decision["eligible"] is True
    assert decision["run_id"] == task.id
    assert decision["same_run_only"] is True
    assert decision["requires_explicit_run_id"] is True
    assert decision["required_recovery_mode"] == "rerun_from_checkpoint"
    assert user_stopped_run_is_resumable(task) is True
    # Ordinary candidate selection still treats CANCELLED as closed. Recovery
    # must name the exact run and use the typed rerun mode.
    assert _is_dispatch_runner_candidate(task) is False


def test_manual_cancel_is_not_resume_candidate() -> None:
    task = _task()
    task.status = TaskStatus.CANCELLED.value
    task.failure_type = FailureType.CANCELLED.value
    task.attributes = {
        "cancel_subagents": {
            "reason": "管理员取消",
            "previous_status": TaskStatus.RUNNING.value,
        }
    }

    decision = user_stopped_resume_eligibility(task)

    assert decision["eligible"] is False
    assert "not_conversation_user_stop" in decision["blockers"]


def test_explicit_recovery_dispatch_selects_the_same_user_stopped_run() -> None:
    task = _task()
    task.status = TaskStatus.CANCELLED.value
    task.failure_type = FailureType.CANCELLED.value
    task.attributes = {
        "cancel_subagents": {
            "reason": "conversation_user_stop",
            "previous_status": TaskStatus.RUNNING.value,
        }
    }
    ctx = SimpleNamespace(
        include_run_ids=[task.id],
        max_runners=1,
        background_launch_id="",
        recovery_mode="rerun_from_checkpoint",
    )

    selected = _runner_candidates_for_context([task], ctx, runner_max_attempts=1)

    assert [item.id for item in selected] == [task.id]
