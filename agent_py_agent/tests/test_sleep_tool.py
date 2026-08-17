"""模块用途: clock.sleep 工具的单元测试(扫描治理第二步)。

验证: 参数校验(范围/缺任务身份/无账本)、字条写入(wake_queue kind=sleep)、
成功回执含 wake_id; 事件取消桥由 conversation/runtime 测试覆盖。
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from types import SimpleNamespace

from agent_py_agent.agent.agent_core.runtime.sleep_tool import (
    _MAX_SECONDS,
    _MIN_SECONDS,
    SleepTool,
)
from agent_py_agent.agent.runtime_db.repository import RuntimeRepository


def _agent_with_repo(tmp_path: Path) -> SimpleNamespace:
    repo = RuntimeRepository(tmp_path / "home" / "runtime.db")
    return SimpleNamespace(subagents=SimpleNamespace(runtime_db=repo)), repo


def _params(duration: int, task_id: str = "task-1", run_id: str = "run-1") -> dict:
    return {
        "duration_seconds": duration,
        "__run_scope": {"task_id": task_id, "run_id": run_id},
    }


def test_sleep_writes_wake_and_returns_wake_id(tmp_path: Path) -> None:
    agent, repo = _agent_with_repo(tmp_path)
    tool = SleepTool(agent)
    outcome = tool.execute(_params(300))
    assert outcome.ok is True
    payload = json.loads(outcome.output)
    assert payload["ok"] is True
    assert "wake_id" in payload["message"]
    pending = repo.list_pending_wakes()
    assert len(pending) == 1
    assert pending[0]["root_task_id"] == "task-1"
    assert pending[0]["kind"] == "sleep"
    assert abs(pending[0]["next_due_at"] - (time.time() + 300)) < 5


def test_sleep_upserts_single_wake_per_task(tmp_path: Path) -> None:
    agent, repo = _agent_with_repo(tmp_path)
    tool = SleepTool(agent)
    tool.execute(_params(100))
    tool.execute(_params(500))
    pending = repo.list_pending_wakes()
    assert len(pending) == 1  # 同任务一行, 更新而非新增


def test_sleep_rejects_out_of_range(tmp_path: Path) -> None:
    agent, _repo = _agent_with_repo(tmp_path)
    tool = SleepTool(agent)
    for bad in (0, -1, _MAX_SECONDS + 1):
        outcome = tool.execute(_params(bad))
        assert outcome.ok is False
        assert outcome.error_code == "TOOL_INVALID_ARGUMENTS"


def test_sleep_requires_task_identity(tmp_path: Path) -> None:
    agent, _repo = _agent_with_repo(tmp_path)
    tool = SleepTool(agent)
    outcome = tool.execute({"duration_seconds": 60})
    assert outcome.ok is False
    assert outcome.error_code == "TOOL_INVALID_ARGUMENTS"


def test_sleep_requires_runtime_db(tmp_path: Path) -> None:
    agent = SimpleNamespace(subagents=None)
    tool = SleepTool(agent)
    outcome = tool.execute(_params(60))
    assert outcome.ok is False
    assert outcome.error_code == "SUBAGENT_CAPACITY_UNAVAILABLE"


def test_sleep_bounds_accept_min_and_max(tmp_path: Path) -> None:
    agent, repo = _agent_with_repo(tmp_path)
    tool = SleepTool(agent)
    assert tool.execute(_params(_MIN_SECONDS)).ok is True
    assert tool.execute(_params(_MAX_SECONDS)).ok is True
    assert len(repo.list_pending_wakes()) == 1
