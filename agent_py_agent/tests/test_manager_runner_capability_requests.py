"""Runner result tests for pending capability request recovery."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from agent_py_agent.agent.subagents.manager_runner_result_payload import RecordRunnerResultParams
from agent_py_agent.agent.subagents.models import (
    CapabilityRequest,
    SubAgentParsedOutput,
    SubAgentTask,
)
from agent_py_agent.agent.subagents.parsing import parse_subagent_runner_output
from agent_py_agent.agent.subagents.services.runner_result import SubAgentRunnerResultService


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

        def record_learning_candidates(self, task, lessons):
            return []

    return TestManager()


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

    result = capability_manager.record_runner_result(_rrr(
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
    assert capability_task.current_step == "PENDING_CAPABILITY_REQUEST"


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

    result = capability_manager.record_runner_result(_rrr(
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

    result = capability_manager.record_runner_result(_rrr(
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

    result = capability_manager.record_runner_result(_rrr(
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

    result = capability_manager.record_runner_result(_rrr(
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

    result = capability_manager.record_runner_result(_rrr(
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
