
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from agent_py_agent.agent.subagents.manager_runner_results import (
    RecordRunnerResultParams,
    SubAgentRunnerResultMixin,
)
from agent_py_agent.agent.subagents.models import SubAgentTask


def _rrr(run_id: str, **kwargs) -> RecordRunnerResultParams:
    """Create RecordRunnerResultParams with run_id as positional arg."""
    return RecordRunnerResultParams(run_id=run_id, **kwargs)


@pytest.fixture
def mock_manager(tmp_path):
    """创建模拟的 SubAgentRunnerResultMixin 管理器。"""

    class TestManager(SubAgentRunnerResultMixin):
        def __init__(self, tmp_dir):
            self._tmp_dir = tmp_dir
            self._tasks = {}
            self._work_logs = []

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

    return TestManager(tmp_path)


@pytest.fixture
def sample_task(tmp_path):
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
        "attributes": {},
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
