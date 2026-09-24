from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.agent_core.models import AgentRunResult
from agent_py_agent.agent.agent_core.subagent.finalize_helpers import (
    FinalizedRunnerRecordRequest,
    record_finalized_runner_result,
)
from agent_py_agent.agent.subagents.manager import SubAgentManager
from agent_py_agent.agent.subagents.manager_runner_result_payload import RecordRunnerResultParams
from agent_py_agent.agent.subagents.models import SubAgentParsedOutput, SubAgentTask
from agent_py_agent.agent.subagents.runner_result_admission import reject_stale_runner_result
from agent_py_agent.agent.subagents.runner_result_state import (
    RunnerResultFieldParams,
    apply_runner_result_fields,
)


def _task() -> SubAgentTask:
    return SubAgentTask(id="run-1", goal="g", thought="t", plan=["p"])


def _apply(task: SubAgentTask, *, parsed: SubAgentParsedOutput, ok: bool = True, failure_type: str = "") -> None:
    apply_runner_result_fields(
        RunnerResultFieldParams(
            task=task,
            result_meta={
                "ok": ok,
                "message": "",
                "response": "",
                "dry_run": False,
            },
            status_context={
                "status": "",
                "verification_status": "",
                "failure_type": failure_type,
            },
            parsed=parsed,
            now=123.0,
        )
    )


def test_runner_result_state_ignores_unknown_structured_failure_type_when_done() -> None:
    task = _task()
    task.current_step = "模型已生成回复"
    task.current_tool = "write_file"

    _apply(task, parsed=SubAgentParsedOutput(found=True, ok=True, status="DONE", failure_type="INCOMPLETE_OUTPUT"))

    assert task.status == "DONE"
    assert task.failure_type == ""
    assert task.current_step == "已完成"
    assert task.current_tool == ""


def test_runner_result_state_maps_unknown_structured_failure_to_status_reason() -> None:
    task = _task()

    _apply(task, parsed=SubAgentParsedOutput(found=True, ok=True, status="BLOCKED", failure_type="INCOMPLETE_OUTPUT"))

    assert task.status == "BLOCKED"
    assert task.failure_type == "status_blocked"


def test_runner_result_state_displays_typed_capability_wait() -> None:
    task = _task()
    task.capability_requests = [type("Request", (), {"status": "OPEN"})()]
    task.current_step = "模型已生成回复"

    _apply(
        task,
        parsed=SubAgentParsedOutput(found=True, ok=True, status="BLOCKED"),
        failure_type="capability_request",
    )

    assert task.status == "BLOCKED"
    assert task.current_step == "等待父级授权"


def test_runner_result_state_does_not_store_unknown_context_failure_type() -> None:
    task = _task()

    _apply(task, parsed=SubAgentParsedOutput(found=False, ok=False), ok=False, failure_type="error")

    assert task.failure_type == "runner_error"


def test_runner_conflict_records_runtime_diagnostic() -> None:
    events: list[dict[str, object]] = []
    repo = SimpleNamespace(
        runner_result_commit_authority=lambda **_kwargs: {
            "agent_run_id": "agent-1",
            "task_run_id": "task-1",
            "is_current": True,
            "run_status": "done",
            "attempt_status": "done",
        },
        append_event=lambda **kwargs: events.append(kwargs),
    )
    task = _task()
    task.runner_active_attempt_id = "attempt-1"
    rejected = reject_stale_runner_result(
        repo,
        task,
        SimpleNamespace(
            attempt_id="attempt-1", status="FAILED", turn_end_reason="error",
            dry_run=False, structured_output=None,
        ),
    )

    assert rejected is not None and rejected.ok is False
    assert len(events) == 1
    assert events[0]["event_type"] == "closeout_blocked"
    assert events[0]["attempt_id"] == "attempt-1"
    assert events[0]["agent_run_id"] == "agent-1"
    assert events[0]["task_run_id"] == "task-1"
    assert events[0]["payload"]["reason"] == "runner_result_conflict"
    assert events[0]["payload"]["run_status"] == "done"


@pytest.mark.parametrize("reason,status,failure,ok", [
    ("completed", "DONE", "", True),
    ("interrupted", "PENDING", "", False),
    ("max-tokens", "PENDING", "model_error", False),
    ("blocked", "BLOCKED", "status_blocked", False),
    ("aborted", "CANCELLED", "cancelled", False),
    ("error", "FAILED", "runner_error", False),
])
def test_finalized_turn_persists_failure_classification(tmp_path, reason, status, failure, ok):
    manager = SubAgentManager(tmp_path / "subagents")
    task = manager.create_run(goal="整理结果", thought="沿原工作继续", plan=["整理"])
    task.status = "RUNNING"
    task.failure_type = "runner_error"
    task.runner_last_error = "上一轮错误"
    manager.save(task)
    result = record_finalized_runner_result(FinalizedRunnerRecordRequest(
        agent=SimpleNamespace(subagents=manager),
        params=SimpleNamespace(
            run_id=task.id,
            active_attempt_id="",
            result=AgentRunResult(
                prompt="整理结果", response="正文只作展示，不决定成功或失败。",
                backend="test", used_memories=0, turn_end_reason=reason,
            ),
        ),
    ))

    saved = manager.load(task.id)
    assert saved.status == status
    assert saved.failure_type == failure
    assert bool(saved.runner_last_error) is bool(failure)
    assert saved.runner_attempts == 1
    assert saved.turn_end_reason == reason
    assert result.ok is ok
    assert result.runner_last_error == saved.runner_last_error
    payload = json.loads(Path(saved.output_json).read_text())
    archived = json.loads(Path(saved.runner_result_json).read_text())
    assert payload["runner_last_error"] == saved.runner_last_error
    assert archived["runner_last_error"] == saved.runner_last_error
    assert payload["status"] == archived["status"] == status
    assert payload["ok"] is archived["ok"] is ok


@pytest.mark.parametrize("reason,status,failure,expected_failure", [
    ("", "PENDING", "", "runner_error"),
    ("unknown", "PENDING", "", "runner_error"),
    ("interrupted", "FAILED", "", "runner_error"),
    ("interrupted", "PENDING", "model_error", "model_error"),
    ("interrupted", "BLOCKED", "permission_blocked", "permission_blocked"),
    ("interrupted", "BLOCKED", "unknown_failure", "runner_error"),
])
def test_unfinished_result_keeps_unproven_or_explicit_failure(tmp_path, reason, status, failure, expected_failure):
    manager = SubAgentManager(tmp_path / "subagents")
    task = manager.create_run(goal="整理结果", thought="检查失败边界", plan=["整理"])
    result = manager.runner_result.record_runner_result(RecordRunnerResultParams(
        run_id=task.id, dry_run=False, ok=False, message="本轮未完成",
        status=status, turn_end_reason=reason, failure_type=failure,
    ))

    saved = manager.load(task.id)
    assert saved.failure_type == expected_failure
    assert saved.runner_last_error == "本轮未完成"
    assert result.ok is False


# LLM: 真实事故形态：宿主机按"可续跑族"(MODEL_STREAM_INCOMPLETE) 提前结清 attempt
# （agent_attempts.status=done）但把 run 留在 created 等 resume；随后 runner 对该**同一个**
# attempt 回写 FAILED，被终态闸判成冲突 → 无 runner_result、无终态、无 wake，任务永久 RUNNING。
# 这里锁两侧：同一 attempt 的 FAILED/CANCELLED 必须能收口；过期/换代 attempt 仍必须被拒。
# 函数用途: 验证 settled attempt 判定对"精确 attempt 的终态回写"放行、对过期 attempt 保持拒绝。
def test_settled_attempt_accepts_exact_attempt_terminal_closure() -> None:
    from agent_py_agent.agent.subagents.models import TaskStatus
    from agent_py_agent.agent.subagents.runner_result_admission import (
        _runner_result_matches_settled_attempt,
    )

    def _params(status: str, turn_end_reason: str) -> RecordRunnerResultParams:
        return RecordRunnerResultParams(
            run_id="run-1",
            dry_run=False,
            ok=False,
            message="",
            status=status,
            turn_end_reason=turn_end_reason,
        )

    # 事故形态：attempt 已 done、run 仍 created、runner 回写 FAILED → 接受（收口为 FAILED）。
    assert _runner_result_matches_settled_attempt(
        "created",
        "done",
        _params(TaskStatus.FAILED.value, "error"),
    )
    # 取消（turn_end=aborted 映射 CANCELLED）同样接受。
    assert _runner_result_matches_settled_attempt(
        "created",
        "done",
        _params(TaskStatus.CANCELLED.value, "aborted"),
    )
    # 可续跑族（PENDING/BLOCKED）原规则不变。
    assert _runner_result_matches_settled_attempt(
        "created",
        "done",
        _params(TaskStatus.BLOCKED.value, "blocked"),
    )
    # run 已终结（done/done）不再需要这条路径。
    assert not _runner_result_matches_settled_attempt(
        "done",
        "done",
        _params(TaskStatus.FAILED.value, "error"),
    )
    # attempt 仍在跑时不属于"已结清"，仍走原路径。
    assert not _runner_result_matches_settled_attempt(
        "created",
        "running",
        _params(TaskStatus.FAILED.value, "error"),
    )
    # 状态与 turn_end 不一致（拿别的终态来冒充）必须拒绝。
    assert not _runner_result_matches_settled_attempt(
        "created",
        "done",
        _params(TaskStatus.FAILED.value, "blocked"),
    )
