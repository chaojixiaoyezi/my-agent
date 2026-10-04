"""pwf：子代理组宿主数据私有写入用例。

验证范围：debug_trace 调试账与明细、子代理任务工作日志（actions/records）。
统一在 umask 0o022 下断言：新文件 0600、新目录 0700；预置 0644 文件写一次后收紧到
0600；已有内容逐字节保留。
"""
from __future__ import annotations

import os
import stat
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.subagents.debug_trace import (
    SubAgentDebugTraceRequest,
    write_subagent_debug_trace,
)


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


@pytest.fixture(autouse=True)
def _fixed_umask():
    previous = os.umask(0o022)
    yield
    os.umask(previous)


def test_debug_trace_is_private_and_tightens(tmp_path: Path) -> None:
    manager = SimpleNamespace(workspace=tmp_path / "ws", debug_trace_level=1)
    request = SubAgentDebugTraceRequest(manager=manager, level=1, event_type="task_created")

    trace_file = write_subagent_debug_trace(request)
    assert trace_file is not None
    assert _mode(trace_file) == 0o600
    assert _mode(trace_file.parent) == 0o700

    # 预置宽权限后追加一条：文件收紧到 0600，旧行逐字节保留。
    os.chmod(trace_file, 0o644)
    before = trace_file.read_bytes()
    write_subagent_debug_trace(request)
    assert trace_file.read_bytes().startswith(before)
    assert _mode(trace_file) == 0o600
    assert _mode(trace_file.parent) == 0o700


def test_task_work_log_is_private_and_tightens(tmp_path: Path) -> None:
    from agent_py_agent.agent.subagents.services.actions.records import append_task_work_log

    manager = SimpleNamespace(indexing=SimpleNamespace(log_local_record=lambda **kwargs: None))
    log_path = tmp_path / "tasks" / "child-1" / "WORK_LOG.md"
    task = SimpleNamespace(
        work_log_file=str(log_path),
        id="child-1",
        goal="示例目标",
        status="RUNNING",
        verification_status="UNVERIFIED",
    )

    append_task_work_log(manager, task, "第一条")

    text = log_path.read_text(encoding="utf-8")
    assert text.startswith("# WORK_LOG\n\n- ")
    assert text.endswith("第一条\n")
    assert _mode(log_path) == 0o600
    assert _mode(log_path.parent) == 0o700

    os.chmod(log_path, 0o644)
    before = log_path.read_bytes()
    append_task_work_log(manager, task, "第二条")
    assert log_path.read_bytes().startswith(before)
    assert _mode(log_path) == 0o600
    assert _mode(log_path.parent) == 0o700
