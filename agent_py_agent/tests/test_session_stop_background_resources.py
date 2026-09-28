"""任务3 后半截合同单测：会话内 /stop 能回收被中断任务留下的后台资源。

来源：my-agent-4 开发交流板任务 3 后半截（dsh-be 的 R10 深度验收）。回合被 /interrupt 后托管后台进程
按设计继续运行；此时同会话再执行 /stop，原先直接回"当前没有运行中的内容"，用户无处回收。

复现方法:
    cd <worktree> && PYTHONPATH=. python3 -m pytest agent_py_agent/tests/test_session_stop_background_resources.py -q

变异验证：把 _execute_local_stop 里 `if not request_id:` 改回直接返回"当前没有运行中的内容"，
无回合回收用例变红；把 session_background_processes 的 thread_id 精确匹配放宽成不过滤，
跨会话隔离用例变红。
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from agent_py_agent.agent.gateway_parts.background_resource_report import (
    session_background_processes,
)
from agent_py_agent.agent.tooling import background_process_launch as launch
from agent_py_agent.agent.tooling.process_scope import ProcessAccessScope, ProcessExecutionScope
from agent_py_agent.agent.tooling.process_session_store import (
    ProcessSessionStore,
    process_session_store_root,
)
from agent_py_agent.cli.chat_parts.control_runtime import (
    ChatControlExecution,
    ChatControlState,
    _execute_local_stop,
)
from agent_py_agent.tests._managed_process_harness import managed_request


class _FakeCommand:
    operation = "stop"


def _seed_running(tmp_path: Path, session_id: str, *, thread: str, owner: str) -> dict[str, object]:
    """按生产预留路径建一条 running 记录（沙箱拿不到出生身份，只对该项打桩）。

    store 根显式用生产同一个函数算：ShellTool 写记录时就是
    `process_session_store_root(effective_workspace_root, owner_home)`，测试必须按同一口径落盘，
    否则会出现"写在这里、列在那里"的假失败。
    """
    request = managed_request(
        tmp_path,
        access_scope=ProcessAccessScope("owner-test", thread, owner),
        execution_scope=ProcessExecutionScope(owner, thread, "task-1", "run-1", "attempt-1"),
        store_root=process_session_store_root(tmp_path, owner),
    )
    with patch.object(launch, "capture_process_birth_token", lambda _pid: "launcher-birth"):
        record = launch._reservation(request, session_id)
    record.update(
        pid=12345, pid_birth_token="host-birth", child_pid=54321, child_pid_birth_token="child-birth",
        child_launch_started=True, started_at=1.0, status="running",
    )
    store = ProcessSessionStore(request.store_root)
    return store.write(record)


class _FakeAgent:
    """只提供 _stop_session_background_resources 需要的字段；workspace 用生产同一个口径。"""

    def __init__(self, root: Path, owner_home: Path) -> None:
        self.root = str(root)
        # 写入端（ShellTool）用 effective_workspace_root 算 store 根，测试必须给同一字段。
        self.effective_workspace_root = Path(root)
        self.home_paths = type("H", (), {"owner_home_dir": str(owner_home)})()


def test_session_filter_keeps_only_exact_thread():
    rows = [
        {"session_id": "a", "thread_id": "t-1"},
        {"session_id": "b", "thread_id": "t-2"},
        {"session_id": "c", "thread_id": ""},
    ]
    assert [row["session_id"] for row in session_background_processes(rows, "t-1")] == ["a"]
    # 空 thread 不能当选"全部"：否则会误停同 owner 其它窗口的资源。
    assert session_background_processes(rows, "") == []


def test_stop_without_running_turn_reclaims_session_resources(tmp_path: Path):
    owner = tmp_path / "owner"
    _seed_running(tmp_path, "bg-session-stop", thread="t-stop", owner=str(owner))
    agent = _FakeAgent(tmp_path, owner)
    execution = ChatControlExecution(
        agent, False, ChatControlState(
            running=False, queued_count=0, prompt="", started_at=0.0,
            session_id="t-stop", request_id="", local_run=None,
        ),
    )

    result = _execute_local_stop(execution, _FakeCommand())

    # 关键：不再回"当前没有运行中的内容"，而是真的去回收本会话资源。
    assert "没有运行中的内容" not in result.message
    assert result.kind == "stop"
    assert "已受理停止 1 个后台资源" in result.message
    # 回执要列出实际停掉的资源（pid + task/run 归属），不能只给一个数量。
    assert "pid 54321" in result.message
    assert "task task-1" in result.message
    assert "run run-1" in result.message
    assert "尚未确认退出" in result.message
    # 测试里没有真实 host 去回收进程，所以这里必须如实报"未确认退出"，不能伪报已停。
    assert result.ok is False
    assert result.error_code == "TASK_RESOURCE_STOP_UNCONFIRMED"


def test_stop_lists_stopped_resources_with_pid_and_ownership(tmp_path: Path):
    """全停成功路径同样要有明细：pid + task/run 归属，且不能写成"未确认"。"""
    owner = tmp_path / "owner-ok"
    _seed_running(tmp_path, "bg-session-ok", thread="t-ok", owner=str(owner))
    agent = _FakeAgent(tmp_path, owner)
    execution = ChatControlExecution(
        agent, False, ChatControlState(
            running=False, queued_count=0, prompt="", started_at=0.0,
            session_id="t-ok", request_id="", local_run=None,
        ),
    )
    with patch(
        "agent_py_agent.agent.gateway_parts.background_resource_report.stop_background_processes",
        return_value=[{"session_id": "bg-session-ok", "stopped": True, "reason": ""}],
    ):
        result = _execute_local_stop(execution, _FakeCommand())

    assert result.ok is True
    assert "已停止本会话遗留的 1 个后台资源" in result.message
    assert "pid 54321" in result.message
    assert "task task-1 / run run-1" in result.message
    assert "尚未确认退出" not in result.message


def test_stop_without_running_turn_and_no_resources_still_says_none(tmp_path: Path):
    owner = tmp_path / "owner-empty"
    agent = _FakeAgent(tmp_path, owner)
    execution = ChatControlExecution(
        agent, False, ChatControlState(
            running=False, queued_count=0, prompt="", started_at=0.0,
            session_id="t-empty", request_id="", local_run=None,
        ),
    )

    result = _execute_local_stop(execution, _FakeCommand())

    assert result.ok is False
    assert "没有运行中的内容" in result.message


def test_stop_reports_unknown_when_store_unreadable(tmp_path: Path):
    """登记表读不了时不能报成功，也不能报"没有内容"。"""
    agent = _FakeAgent(tmp_path, tmp_path / "owner-x")
    execution = ChatControlExecution(
        agent, False, ChatControlState(
            running=False, queued_count=0, prompt="", started_at=0.0,
            session_id="t-x", request_id="", local_run=None,
        ),
    )
    with patch(
        "agent_py_agent.agent.gateway_parts.background_resource_report.list_running_background_processes",
        side_effect=OSError("unreadable"),
    ):
        result = _execute_local_stop(execution, _FakeCommand())

    assert result.ok is False
    assert result.error_code == "TASK_RESOURCE_STOP_UNCONFIRMED"
    assert "没有运行中的内容" not in result.message
