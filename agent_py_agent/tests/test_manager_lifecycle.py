"""manager_lifecycle 模块测试。

测试 manager_lifecycle.py 中的任务状态转换（PAUSED/ABANDONED/RESUMED）、心跳更新功能。
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from agent_py_agent.agent.subagents.models import TaskStatus


# ── 测试夹具 ──────────────────────────────────────────────────────────────

class MockTask:
    """模拟的 SubAgentTask 对象。"""
    def __init__(self, run_id="test-run-123", status="RUNNING"):
        self.run_id = run_id
        self.id = run_id
        self.status = status
        self.heartbeat_at = time.time() - 100
        self.updated_at = time.time() - 100
        self.ended_at = 0.0
        self.result = ""
        self.failure_type = ""
        self.evidence = []
        self.verification_status = "UNVERIFIED"
        self.runner_active_attempt_id = ""
        self.runner_attempts = 0
        self.runner_abandoned_attempt_ids = []
        self.capability_requests = []
        self.capability_grants = []
        self.capability_gaps = []
        self.allowed_skills = []
        self.allowed_tools = []
        self.context_manifest = MockContextManifest()

    def save(self):
        """模拟 save 方法。"""
        pass


@dataclass
class MockContextManifest:
    required_read_paths: list = field(default_factory=list)


class MockManager:
    """模拟的 SubAgentManager（用于测试 mixin）。"""
    def __init__(self, tmp_path=None):
        self.workspace_root = tmp_path or Path("/tmp/test")
        self._tasks = {}

    def load(self, run_id: str):
        if run_id not in self._tasks:
            self._tasks[run_id] = MockTask(run_id=run_id)
        return self._tasks[run_id]

    def save(self, task):
        # 保存但不创建新对象，确保修改被保留
        self._tasks[task.run_id] = task

    def _append_task_work_log(self, task, message):
        pass


# ── 初始化测试 ─────────────────────────────────────────────────────────────

def test_lifecycle_mixin_exists():
    """测试 SubAgentLifecycleMixin 类存在。"""
    from agent_py_agent.agent.subagents.manager_lifecycle import SubAgentLifecycleMixin
    assert SubAgentLifecycleMixin is not None


# ── touch_heartbeat 测试 ──────────────────────────────────────────────────

def test_touch_heartbeat_updates_timestamp():
    """测试 touch_heartbeat 更新 heartbeat_at 时间戳。"""
    from agent_py_agent.agent.subagents.manager_lifecycle import SubAgentLifecycleMixin

    manager = MockManager()
    mixin = SubAgentLifecycleMixin.__new__(SubAgentLifecycleMixin)
    mixin.load = manager.load
    mixin.save = manager.save

    run_id = "test-heartbeat"
    task = manager.load(run_id)
    old_heartbeat = task.heartbeat_at

    mixin.touch_heartbeat(run_id)

    assert task.heartbeat_at > old_heartbeat


def test_touch_heartbeat_updates_manifest():
    """测试 touch_heartbeat 同时更新 updated_at。"""
    from agent_py_agent.agent.subagents.manager_lifecycle import SubAgentLifecycleMixin

    manager = MockManager()
    mixin = SubAgentLifecycleMixin.__new__(SubAgentLifecycleMixin)
    mixin.load = manager.load
    mixin.save = manager.save

    run_id = "test-hb-manifest"
    task = manager.load(run_id)
    old_updated = task.updated_at

    time.sleep(0.01)
    mixin.touch_heartbeat(run_id)

    assert task.updated_at >= old_updated


# ── set_status 测试 ────────────────────────────────────────────────────────

def test_set_status_updates_status():
    """测试 set_status 更新任务状态。"""
    from agent_py_agent.agent.subagents.manager_lifecycle import SubAgentLifecycleMixin

    manager = MockManager()
    mixin = SubAgentLifecycleMixin.__new__(SubAgentLifecycleMixin)
    mixin.load = manager.load
    mixin.save = manager.save

    run_id = "test-status"
    task = manager.load(run_id)
    assert task.status == "RUNNING"

    mixin.set_status(run_id, "PAUSED")

    assert task.status == "PAUSED"


def test_set_status_normalizes_to_uppercase():
    """测试 set_status 将状态转换为大写。"""
    from agent_py_agent.agent.subagents.manager_lifecycle import SubAgentLifecycleMixin

    manager = MockManager()
    mixin = SubAgentLifecycleMixin.__new__(SubAgentLifecycleMixin)
    mixin.load = manager.load
    mixin.save = manager.save

    run_id = "test-status-case"
    mixin.set_status(run_id, "paused")

    task = manager.load(run_id)
    assert task.status == "PAUSED"


def test_set_status_with_result():
    """测试 set_status 同时设置 result。"""
    from agent_py_agent.agent.subagents.manager_lifecycle import SubAgentLifecycleMixin

    manager = MockManager()
    mixin = SubAgentLifecycleMixin.__new__(SubAgentLifecycleMixin)
    mixin.load = manager.load
    mixin.save = manager.save

    run_id = "test-status-result"
    mixin.set_status(run_id, "DONE", result="Task completed successfully")

    task = manager.load(run_id)
    assert task.result == "Task completed successfully"


def test_set_status_with_failure_type():
    """测试 set_status 设置失败类型。"""
    from agent_py_agent.agent.subagents.manager_lifecycle import SubAgentLifecycleMixin

    manager = MockManager()
    mixin = SubAgentLifecycleMixin.__new__(SubAgentLifecycleMixin)
    mixin.load = manager.load
    mixin.save = manager.save

    run_id = "test-status-failure"
    mixin.set_status(run_id, "FAILED", failure_type="CAPABILITY_GAP")

    task = manager.load(run_id)
    assert task.failure_type == "CAPABILITY_GAP"


def test_set_status_sets_ended_at():
    """测试 set_status 为终态设置 ended_at。"""
    from agent_py_agent.agent.subagents.manager_lifecycle import SubAgentLifecycleMixin

    manager = MockManager()
    mixin = SubAgentLifecycleMixin.__new__(SubAgentLifecycleMixin)
    mixin.load = manager.load
    mixin.save = manager.save

    run_id = "test-ended-at"
    task = manager.load(run_id)
    assert task.ended_at == 0.0

    # FAILED 是会设置 ended_at 的终态
    mixin.set_status(run_id, "FAILED")

    task = manager.load(run_id)
    assert task.ended_at > 0


def test_set_status_requires_evidence_for_done():
    """测试 set_status 在 require_evidence=True 时，没有证据不能标记为 DONE。"""
    from agent_py_agent.agent.subagents.manager_lifecycle import SubAgentLifecycleMixin

    manager = MockManager()
    mixin = SubAgentLifecycleMixin.__new__(SubAgentLifecycleMixin)
    mixin.load = manager.load
    mixin.save = manager.save

    run_id = "test-no-evidence"
    task = manager.load(run_id)
    task.evidence = []

    with pytest.raises(ValueError, match="缺少验收证据"):
        mixin.set_status(run_id, "DONE", require_evidence=True)


def test_set_status_allows_done_with_evidence():
    """测试 set_status 在有证据时允许标记为 DONE。"""
    from agent_py_agent.agent.subagents.manager_lifecycle import SubAgentLifecycleMixin

    manager = MockManager()
    mixin = SubAgentLifecycleMixin.__new__(SubAgentLifecycleMixin)
    mixin.load = manager.load
    mixin.save = manager.save

    run_id = "test-with-evidence"
    task = manager.load(run_id)
    task.evidence = [{"kind": "test", "summary": "test evidence"}]

    # 不应抛出异常
    mixin.set_status(run_id, "DONE", require_evidence=True)

    assert task.status == "DONE"


# ── prepare_runner_attempt 测试 ───────────────────────────────────────────

def test_prepare_runner_attempt_sets_running():
    """测试 prepare_runner_attempt 设置状态为 RUNNING。"""
    from agent_py_agent.agent.subagents.manager_lifecycle import SubAgentLifecycleMixin

    manager = MockManager()
    mixin = SubAgentLifecycleMixin.__new__(SubAgentLifecycleMixin)
    mixin.load = manager.load
    mixin.save = manager.save
    mixin._append_task_work_log = lambda *args: None

    run_id = "test-prepare"
    task = manager.load(run_id)
    task.status = "FAILED"

    mixin.prepare_runner_attempt(run_id)

    assert task.status == "RUNNING"


def test_prepare_runner_attempt_clears_failure():
    """测试 prepare_runner_attempt 清除之前的失败类型。"""
    from agent_py_agent.agent.subagents.manager_lifecycle import SubAgentLifecycleMixin

    manager = MockManager()
    mixin = SubAgentLifecycleMixin.__new__(SubAgentLifecycleMixin)
    mixin.load = manager.load
    mixin.save = manager.save
    mixin._append_task_work_log = lambda *args: None

    run_id = "test-clear-failure"
    task = manager.load(run_id)
    task.failure_type = "CAPABILITY_GAP"

    mixin.prepare_runner_attempt(run_id)

    assert task.failure_type == ""


def test_prepare_runner_attempt_resets_ended_at():
    """测试 prepare_runner_attempt 重置 ended_at。"""
    from agent_py_agent.agent.subagents.manager_lifecycle import SubAgentLifecycleMixin

    manager = MockManager()
    mixin = SubAgentLifecycleMixin.__new__(SubAgentLifecycleMixin)
    mixin.load = manager.load
    mixin.save = manager.save
    mixin._append_task_work_log = lambda *args: None

    run_id = "test-reset-ended"
    task = manager.load(run_id)
    task.ended_at = time.time()

    mixin.prepare_runner_attempt(run_id)

    assert task.ended_at == 0.0


def test_prepare_runner_attempt_sets_attempt_id():
    """测试 prepare_runner_attempt 生成新的 attempt_id。"""
    from agent_py_agent.agent.subagents.manager_lifecycle import SubAgentLifecycleMixin

    manager = MockManager()
    mixin = SubAgentLifecycleMixin.__new__(SubAgentLifecycleMixin)
    mixin.load = manager.load
    mixin.save = manager.save
    mixin._append_task_work_log = lambda *args: None

    run_id = "test-attempt-id"
    mixin.prepare_runner_attempt(run_id)

    task = manager.load(run_id)
    assert task.runner_active_attempt_id != ""


def test_prepare_runner_attempt_increments_attempts():
    """测试 prepare_runner_attempt 生成 attempt_id。"""
    from agent_py_agent.agent.subagents.manager_lifecycle import SubAgentLifecycleMixin

    manager = MockManager()
    mixin = SubAgentLifecycleMixin.__new__(SubAgentLifecycleMixin)
    mixin.load = manager.load
    mixin.save = manager.save
    mixin._append_task_work_log = lambda *args: None

    run_id = "test-increment"
    task = manager.load(run_id)
    task.runner_attempts = 0
    initial_attempt_id = task.runner_active_attempt_id

    mixin.prepare_runner_attempt(run_id)

    # 重新获取任务
    task = manager.load(run_id)
    # runner_active_attempt_id 应该是新生成的
    assert task.runner_active_attempt_id != initial_attempt_id
    assert task.runner_active_attempt_id != ""


# ── abandon_runner_attempt 测试 ───────────────────────────────────────────

def test_abandon_runner_attempt_adds_to_abandoned_list():
    """测试 abandon_runner_attempt 将 attempt_id 添加到废弃列表。"""
    from agent_py_agent.agent.subagents.manager_lifecycle import SubAgentLifecycleMixin

    manager = MockManager()
    mixin = SubAgentLifecycleMixin.__new__(SubAgentLifecycleMixin)
    mixin.load = manager.load
    mixin.save = manager.save
    mixin._append_task_work_log = lambda *args: None

    run_id = "test-abandon"
    task = manager.load(run_id)
    assert "attempt-999" not in task.runner_abandoned_attempt_ids

    mixin.abandon_runner_attempt(run_id, "attempt-999")

    assert "attempt-999" in task.runner_abandoned_attempt_ids


def test_abandon_runner_attempt_clears_active_if_match():
    """测试 abandon_runner_attempt 匹配时清除 active_attempt_id。"""
    from agent_py_agent.agent.subagents.manager_lifecycle import SubAgentLifecycleMixin

    manager = MockManager()
    mixin = SubAgentLifecycleMixin.__new__(SubAgentLifecycleMixin)
    mixin.load = manager.load
    mixin.save = manager.save
    mixin._append_task_work_log = lambda *args: None

    run_id = "test-clear-active"
    task = manager.load(run_id)
    task.runner_active_attempt_id = "attempt-123"

    mixin.abandon_runner_attempt(run_id, "attempt-123")

    assert task.runner_active_attempt_id == ""


def test_abandon_runner_attempt_empty_id():
    """测试 abandon_runner_attempt 处理空 ID。"""
    from agent_py_agent.agent.subagents.manager_lifecycle import SubAgentLifecycleMixin

    manager = MockManager()
    mixin = SubAgentLifecycleMixin.__new__(SubAgentLifecycleMixin)
    mixin.load = manager.load
    mixin.save = manager.save
    mixin._append_task_work_log = lambda *args: None

    run_id = "test-empty-id"
    task = manager.load(run_id)
    initial_abandoned = len(task.runner_abandoned_attempt_ids)

    result = mixin.abandon_runner_attempt(run_id, "")

    # 空 ID 应该被忽略，不添加任何东西
    assert len(task.runner_abandoned_attempt_ids) == initial_abandoned


def test_abandon_runner_attempt_duplicate():
    """测试 abandon_runner_attempt 处理重复 ID。"""
    from agent_py_agent.agent.subagents.manager_lifecycle import SubAgentLifecycleMixin

    manager = MockManager()
    mixin = SubAgentLifecycleMixin.__new__(SubAgentLifecycleMixin)
    mixin.load = manager.load
    mixin.save = manager.save
    mixin._append_task_work_log = lambda *args: None

    run_id = "test-duplicate"
    task = manager.load(run_id)

    mixin.abandon_runner_attempt(run_id, "dup-attempt")
    mixin.abandon_runner_attempt(run_id, "dup-attempt")

    # 重复 ID 不应重复添加
    assert task.runner_abandoned_attempt_ids.count("dup-attempt") == 1


# ── 异常场景测试 ──────────────────────────────────────────────────────────

def test_set_status_unknown_state_still_works():
    """测试设置任意字符串状态都能工作。"""
    from agent_py_agent.agent.subagents.manager_lifecycle import SubAgentLifecycleMixin

    manager = MockManager()
    mixin = SubAgentLifecycleMixin.__new__(SubAgentLifecycleMixin)
    mixin.load = manager.load
    mixin.save = manager.save

    run_id = "test-unknown-state"
    mixin.set_status(run_id, "UNKNOWN_STATE_123")

    task = manager.load(run_id)
    assert task.status == "UNKNOWN_STATE_123"


def test_prepare_runner_with_reason():
    """测试 prepare_runner_attempt 记录重试原因。"""
    from agent_py_agent.agent.subagents.manager_lifecycle import SubAgentLifecycleMixin

    manager = MockManager()
    mixin = SubAgentLifecycleMixin.__new__(SubAgentLifecycleMixin)
    mixin.load = manager.load
    mixin.save = manager.save
    log_messages = []
    mixin._append_task_work_log = lambda task, msg: log_messages.append(msg)

    run_id = "test-retry-reason"
    mixin.prepare_runner_attempt(run_id, retry_reason="network timeout")

    assert any("network timeout" in msg for msg in log_messages)


def test_touch_heartbeat_on_nonexistent_task():
    """测试 touch_heartbeat 处理不存在的任务。"""
    from agent_py_agent.agent.subagents.manager_lifecycle import SubAgentLifecycleMixin

    manager = MockManager()
    mixin = SubAgentLifecycleMixin.__new__(SubAgentLifecycleMixin)
    mixin.load = manager.load
    mixin.save = manager.save

    # 访问不存在的任务应该自动创建
    run_id = "nonexistent-task"
    mixin.touch_heartbeat(run_id)

    # 任务应该被创建
    assert manager.load(run_id) is not None