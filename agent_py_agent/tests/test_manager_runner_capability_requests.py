"""Runner result tests for pending capability request recovery."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from agent_py_agent.agent.subagents.manager_runner_result_payload import RecordRunnerResultParams
from agent_py_agent.agent.subagents.model_capabilities import (
    capability_request_counts_as_open,
    capability_request_requires_parent_resolution,
    capability_request_suppresses_duplicate,
    is_pending_capability_status,
)
from agent_py_agent.agent.subagents.models import (
    CapabilityGrant,
    CapabilityRequest,
    SubAgentParsedOutput,
    SubAgentTask,
)
from agent_py_agent.agent.subagents.parsing import parse_subagent_runner_output
from agent_py_agent.agent.subagents.services.runner_result_service import (
    SubAgentRunnerResultService,
)


def _rrr(run_id: str, **kwargs) -> RecordRunnerResultParams:
    """创建 RecordRunnerResultParams。"""
    return RecordRunnerResultParams(run_id=run_id, **kwargs)


@pytest.fixture
def capability_manager():
    """创建只覆盖 runner result 能力申请路径的轻量 manager。"""

    class TestManager:
        def __init__(self):
            self._tasks = {}
            self._work_logs = []
            # 与生产 SubAgentManager 对齐:验收机器执行的沙箱门(空 = 普通执行)。
            self.owner_scope_root = ""
            self.actions = SimpleNamespace(_append_task_work_log=self._append_task_work_log)
            self.indexing = SimpleNamespace(index_runner_result=self._index_runner_result)
            self.memory_candidates = SimpleNamespace(record_result_candidates=self.record_result_candidates)
            self.runner_result = SubAgentRunnerResultService(self)

        def record_runner_result(self, params: RecordRunnerResultParams):
            return self.runner_result.record_runner_result(params)

        def load(self, run_id: str) -> SubAgentTask:
            return self._tasks.get(run_id)

        def save(self, task: SubAgentTask) -> None:
            self._tasks[task.id] = task

        def _build_work_order_paths(self, run_id: str, task_dir=None):
            return {}

        def _append_task_work_log(self, task: SubAgentTask, message: str):
            self._work_logs.append(message)

        def _index_runner_result(self, result, payload):
            pass

        def record_result_candidates(self, task, *, lessons, findings):
            return []

    return TestManager()


def test_legacy_capability_request_status_is_not_current_open_request():
    assert capability_request_counts_as_open("OPEN") is True
    assert capability_request_counts_as_open("open") is False
    assert capability_request_suppresses_duplicate("granted") is False
    assert is_pending_capability_status("pending_capability_request") is False
    assert capability_request_counts_as_open("RESOLVED") is False
    assert capability_request_suppresses_duplicate("RESOLVED") is False
    assert capability_request_requires_parent_resolution("RESOLVED") is True
    assert capability_request_requires_parent_resolution("pending") is True


@pytest.fixture
def capability_task(tmp_path):
    """创建带完整 runner 文件路径的能力申请任务。"""
    task = MagicMock(spec=SubAgentTask)
    for field_name, value in _sample_task_fields(tmp_path).items():
        setattr(task, field_name, value)
    return task


def _sample_task_fields(tmp_path) -> dict:
    return {
        "id": "run-123",
        "status": "RUNNING",
        "verification_status": "UNVERIFIED",
        "failure_type": "",
        "result": "",
        "ended_at": 0.0,
        "updated_at": 0.0,
        "heartbeat_at": 0.0,
        "runner_attempts": 0,
        "runner_last_attempt_at": 0.0,
        "runner_last_error": "",
        "runner_active_attempt_id": "",
        "runner_abandoned_attempt_ids": [],
        "used_tools": [],
        "used_skills": [],
        "evidence": [],
        "evidence_packets": [],
        "findings": [],
        "evidence_refs": [],
        "artifact_refs": [],
        "blockers": [],
        "progress": 0.0,
        "current_step": "",
        "latest_summary": "",
        "budget_used": {},
        "checkpoint_ref": "",
        "capability_requests": [],
        "capability_grants": [],
        "allowed_tools": ["tool_a", "tool_b"],
        "allowed_skills": ["skill_x"],
        **_sample_task_path_fields(tmp_path),
    }


def _sample_task_path_fields(tmp_path) -> dict:
    return {
        "runner_prompt_file": str(tmp_path / "prompt.txt"),
        "runner_response_file": str(tmp_path / "response.txt"),
        "runner_result_file": str(tmp_path / "result.md"),
        "runner_result_json": str(tmp_path / "result.json"),
        "output_json": str(tmp_path / "output.json"),
        "status_report_json": str(tmp_path / "status_report.json"),
        "debrief_file": str(tmp_path / "debrief.md"),
        "execution_context_file": str(tmp_path / "context.md"),
        "execution_context_json": str(tmp_path / "context.json"),
        "task_dir": str(tmp_path),
        "goal": "测试任务",
        "thought": "",
        "plan": [],
        "agent_name": "test-agent",
        "role": "general",
        "owner": "tester",
        "supervisor": "",
        "final_owner": "",
        "parent_id": "",
        "root_id": "",
        "depth": 0,
    }


def test_record_runner_result_pending_capability_stays_blocked(capability_manager, capability_task):
    """测试 pending capability 状态不会被写成等待收口。"""
    capability_manager._tasks[capability_task.id] = capability_task
    parsed = SubAgentParsedOutput(
        found=True,
        ok=True,
        status="PENDING_CAPABILITY_REQUEST",
        summary="需要申请 controlled_exec",
        capability_requests=[],
        blocked_reason="",
    )

    result = capability_manager.runner_result.record_runner_result(_rrr(
        run_id="run-123",
        dry_run=False,
        ok=True,
        message="等待能力申请",
        structured_output=parsed,
    ))

    assert result.status == "BLOCKED"
    assert result.verification_status == "UNVERIFIED"
    assert capability_task.status == "BLOCKED"
    assert capability_task.failure_type == "capability_request"
    assert capability_task.status == "BLOCKED"
    assert capability_task.current_step == "等待父级授权"


def test_record_runner_result_does_not_treat_old_tool_wait_status_as_capability_request(
    capability_manager,
    capability_task,
):
    """旧 WAITING_FOR_TOOL 状态只能 fail closed，不能自动变成能力申请。"""
    capability_manager._tasks[capability_task.id] = capability_task
    parsed = SubAgentParsedOutput(
        found=True,
        ok=True,
        status="WAITING_FOR_TOOL",
        summary="等待某个工具，但没有结构化 capability_request。",
        capability_requests=[],
        blocked_reason="",
    )

    result = capability_manager.runner_result.record_runner_result(_rrr(
        run_id="run-123",
        dry_run=False,
        ok=True,
        message="旧状态别名",
        structured_output=parsed,
    ))

    assert result.status == "BLOCKED"
    assert capability_task.status == "BLOCKED"
    assert capability_task.failure_type != "capability_request"
    assert capability_task.capability_requests == []


def test_record_runner_result_requires_explicit_pending_capability_request(capability_manager, capability_task):
    """pending_steps 不会被系统猜成 capability request。"""
    capability_manager._tasks[capability_task.id] = capability_task
    parsed = parse_subagent_runner_output("""[SUBAGENT_RESULT]
{
  "status": "PENDING_CAPABILITY_REQUEST",
  "summary": "需要申请 controlled_exec",
  "pending_steps": [
    {"action": "request_controlled_exec_grant", "status": "in_progress"},
    {"action": "execute_pwd", "status": "pending"},
    {"action": "execute_python3_large_output", "status": "pending"},
    {"action": "execute_rm_sentinel", "status": "pending"}
  ]
}
[/SUBAGENT_RESULT]""")

    result = capability_manager.runner_result.record_runner_result(_rrr(
        run_id="run-123",
        dry_run=False,
        ok=True,
        message="等待能力申请",
        structured_output=parsed,
    ))

    assert result.status == "BLOCKED"
    output = json.loads(Path(capability_task.output_json).read_text(encoding="utf-8"))
    assert output["structured_output"]["capability_request_count"] == 0
    assert output["next_action"] != "route_capability_request"
    assert capability_task.capability_requests == []


def test_record_runner_result_keeps_tool_created_open_request_blocked(capability_manager, capability_task):
    """capability_request 工具写入的 OPEN request 不能被 DONE 收口误清理。"""
    capability_task.capability_requests.append(
        CapabilityRequest(
            id="capreq-tool",
            from_run_id=capability_task.id,
            problem="需要 controlled_exec 才能验证真实命令。",
            needed_capability="controlled_exec",
            requested_tools=["controlled_exec"],
            status="OPEN",
        )
    )
    capability_manager._tasks[capability_task.id] = capability_task
    parsed = SubAgentParsedOutput(
        found=True,
        ok=True,
        status="DONE",
        summary="写了 capability_request.json，但没有真正授权。",
        capability_requests=[],
        blocked_reason="",
    )

    result = capability_manager.runner_result.record_runner_result(_rrr(
        run_id="run-123",
        dry_run=False,
        ok=True,
        message="不应进入验收",
        structured_output=parsed,
    ))

    assert result.status == "BLOCKED"
    assert capability_task.status == "BLOCKED"
    assert capability_task.failure_type == "capability_request"
    assert capability_task.capability_requests[0].status == "OPEN"
    assert any("route_capability_request" in item for item in capability_task.blockers)


def test_runner_result_counts_tool_created_granted_request_without_model_result_block(
    capability_manager,
    capability_task,
):
    """普通自然回复缺结果块时，runner 仍投影真实工具申请数。"""
    capability_task.capability_requests.append(
        CapabilityRequest(
            id="capreq-tool-granted",
            from_run_id=capability_task.id,
            problem="需要写入任务报告。",
            needed_capability="write_file",
            requested_tools=["write_file"],
            status="GRANTED",
        )
    )
    capability_manager._tasks[capability_task.id] = capability_task

    result = capability_manager.runner_result.record_runner_result(_rrr(
        run_id="run-123",
        dry_run=False,
        ok=True,
        message="runner 本轮结束: completed",
        structured_output=SubAgentParsedOutput(found=False, ok=False),
        response="已经写完报告。",
    ))

    assert result.capability_request_count == 1


def test_unstructured_completed_turn_cannot_close_tool_created_open_request(
    capability_manager,
    capability_task,
):
    """宿主 completed 不能覆盖工具已落盘的 OPEN 申请。"""
    capability_task.capability_requests.append(
        CapabilityRequest(
            id="capreq-unstructured",
            from_run_id=capability_task.id,
            problem="需要写入父级工作区。",
            needed_capability="filesystem",
            requested_tools=["write_file"],
            status="OPEN",
        )
    )
    capability_manager._tasks[capability_task.id] = capability_task

    result = capability_manager.runner_result.record_runner_result(
        _rrr(
            run_id="run-123",
            dry_run=False,
            ok=True,
            message="runner 本轮结束: completed",
            turn_end_reason="completed",
            structured_output=None,
            actual_tools=["capability_request"],
        )
    )

    assert result.status == "BLOCKED"
    assert capability_task.failure_type == "capability_request"
    assert result.turn_end_reason == "blocked"
    assert capability_task.progress < 1.0
    output = json.loads(Path(capability_task.output_json).read_text(encoding="utf-8"))
    assert output["status"] == "BLOCKED"
    assert output["turn_end_reason"] == "blocked"


def test_fresh_grant_after_final_capability_tool_requeues_same_run(
    capability_manager,
    capability_task,
):
    """这一轮申请并获授权后立即收口，应续跑同一 run。"""
    capability_task.runner_last_attempt_at = 100.0
    capability_task.capability_requests.append(
        CapabilityRequest(
            id="capreq-granted",
            from_run_id=capability_task.id,
            problem="需要写入父级工作区。",
            needed_capability="filesystem",
            requested_tools=["write_file"],
            status="GRANTED",
        )
    )
    capability_task.capability_grants.append(
        CapabilityGrant(
            id="capgrant-fresh",
            request_id="capreq-granted",
            grant_to_run_id=capability_task.id,
            tools=["write_file"],
            created_at=101.0,
        )
    )
    capability_manager._tasks[capability_task.id] = capability_task

    result = capability_manager.runner_result.record_runner_result(
        _rrr(
            run_id="run-123",
            dry_run=False,
            ok=True,
            message="runner 本轮结束: completed",
            turn_end_reason="completed",
            structured_output=None,
            actual_tools=["capability_request"],
        )
    )

    assert result.status == "PENDING"
    assert capability_task.failure_type == "capability_request"
    assert result.turn_end_reason == "completed"
    assert capability_task.ended_at == 0.0
    assert capability_task.progress < 1.0


def test_legacy_request_status_does_not_silently_close(capability_manager, capability_task):
    """旧 capability_request 状态不能被当成当前协议的已处理终态。"""
    capability_task.capability_requests.append(
        CapabilityRequest(
            id="capreq-old",
            from_run_id=capability_task.id,
            problem="旧状态残留不应隐藏。",
            needed_capability="controlled_exec",
            requested_tools=["controlled_exec"],
            status="RESOLVED",
        )
    )
    capability_manager._tasks[capability_task.id] = capability_task
    parsed = SubAgentParsedOutput(
        found=True,
        ok=True,
        status="DONE",
        summary="模型声称完成，但旧状态 request 仍需结构化处理。",
        capability_requests=[],
        blocked_reason="",
    )

    result = capability_manager.runner_result.record_runner_result(_rrr(
        run_id="run-123",
        dry_run=False,
        ok=True,
        message="不应进入验收",
        structured_output=parsed,
    ))

    assert result.status == "BLOCKED"
    assert capability_task.status == "BLOCKED"
    assert capability_task.failure_type == "capability_request"
    assert capability_task.capability_requests[0].status == "RESOLVED"


def test_successful_recovery_closes_current_open_request_with_current_status(capability_manager, capability_task):
    """从 capability_request 恢复成功后，用当前 CLOSED 状态关闭旧 OPEN 请求。"""
    capability_task.failure_type = "capability_request"
    capability_task.capability_requests.append(
        CapabilityRequest(
            id="capreq-open",
            from_run_id=capability_task.id,
            problem="等待授权后重跑。",
            needed_capability="controlled_exec",
            requested_tools=["controlled_exec"],
            status="OPEN",
        )
    )
    capability_manager._tasks[capability_task.id] = capability_task
    parsed = SubAgentParsedOutput(
        found=True,
        ok=True,
        status="DONE",
        summary="授权后完成。",
        capability_requests=[],
        blocked_reason="",
    )

    result = capability_manager.runner_result.record_runner_result(_rrr(
        run_id="run-123",
        dry_run=False,
        ok=True,
        message="完成",
        structured_output=parsed,
    ))

    assert result.status == "DONE"
    assert capability_task.status == "DONE"
    assert capability_task.failure_type == ""
    assert capability_task.capability_requests[0].status == "CLOSED"


def test_parse_error_with_tool_created_open_request_stays_capability_blocked(capability_manager, capability_task):
    capability_task.capability_requests.append(
        CapabilityRequest(
            id="capreq-tool",
            from_run_id=capability_task.id,
            problem="需要 controlled_exec 才能继续。",
            needed_capability="controlled_exec",
            requested_tools=["controlled_exec"],
            requested_commands=["pwd"],
            status="OPEN",
        )
    )
    capability_manager._tasks[capability_task.id] = capability_task
    parsed = SubAgentParsedOutput(
        found=True,
        ok=False,
        parse_error="缺少 [/SUBAGENT_RESULT] 结束标记。",
        status="PENDING_CAPABILITY_REQUEST",
        summary="等待父级授权",
        capability_requests=[],
        blocked_reason="",
    )

    result = capability_manager.runner_result.record_runner_result(_rrr(
        run_id="run-123",
        dry_run=False,
        ok=True,
        message="模型结果块被截断",
        structured_output=parsed,
    ))

    assert result.status == "BLOCKED"
    assert result.verification_status == "UNVERIFIED"


def test_record_runner_result_dedupes_parsed_request_against_tool_request(capability_manager, capability_task):
    """工具调用和结构化结果描述同一个能力申请时，只保留一条 request。"""
    capability_task.capability_requests.append(
        CapabilityRequest(
            id="capreq-tool",
            from_run_id=capability_task.id,
            problem="tool already requested controlled_exec",
            needed_capability="controlled_exec",
            capability_type="shell",
            requested_tools=["controlled_exec"],
            requested_commands=["pwd", "python3"],
            path_scope=["/workspace/task"],
            output_budget={"stdout_bytes": 4096},
            status="OPEN",
        )
    )
    capability_manager._tasks[capability_task.id] = capability_task
    parsed = SubAgentParsedOutput(
        found=True,
        ok=True,
        status="BLOCKED",
        summary="same request in final block",
        capability_requests=[{
            "problem": "same request in model JSON",
            "needed_capability": "controlled_exec",
            "capability_type": "shell",
            "requested_tools": ["controlled_exec"],
            "requested_commands": ["python3 -c \"print(1)\"", "pwd"],
            "path_scope": ["/workspace/task"],
            "output_budget": {"stdout_bytes": 4096},
        }],
        blocked_reason="waiting for grant",
    )

    result = capability_manager.runner_result.record_runner_result(_rrr(
        run_id="run-123",
        dry_run=False,
        ok=True,
        message="waiting for grant",
        structured_output=parsed,
    ))

    output = json.loads(Path(capability_task.output_json).read_text(encoding="utf-8"))
    assert result.status == "BLOCKED"
    assert len(capability_task.capability_requests) == 1
    assert output["structured_output"]["capability_request_ids"] == ["capreq-tool"]


def test_parse_error_still_records_actual_tool_facts(capability_manager, capability_task):
    """结果块截断时，runner loop 的 actual_tools 仍应写进 task.used_tools。"""
    capability_task.allowed_tools = ["write_file", "controlled_exec"]
    capability_manager._tasks[capability_task.id] = capability_task
    parsed = SubAgentParsedOutput(
        found=True,
        ok=False,
        parse_error="缺少 [/SUBAGENT_RESULT] 结束标记。",
    )

    result = capability_manager.runner_result.record_runner_result(_rrr(
        run_id="run-123",
        dry_run=False,
        ok=True,
        message="model response cut",
        structured_output=parsed,
        actual_tools=["write_file", "controlled_exec"],
    ))

    output = json.loads(Path(capability_task.output_json).read_text(encoding="utf-8"))
    assert result.status == "BLOCKED"
    assert "controlled_exec" in capability_task.used_tools
    assert "controlled_exec" in output["used_tools"]
    assert output["structured_output"]["actual_tools"] == ["write_file", "controlled_exec"]
    assert capability_task.status == "BLOCKED"
    assert capability_task.failure_type == "structured_output_parse_error"
