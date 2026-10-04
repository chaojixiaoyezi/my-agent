"""pwf：协作与身份组宿主数据私有写入用例。

验证范围：collaboration 协作账（参与者/证据/裁决/请求）与 user_space 运行工作区
（timeline/state/task.yaml/协作种子）。统一在 umask 0o022 下断言：新文件 0600、
新目录 0700；预置 0644 文件写一次后收紧到 0600；已有内容逐字节保留。
"""
from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from agent_py_agent.agent.collaboration import CollaborationStore
from agent_py_agent.agent.user_space.run_workspace import (
    EnsureRunWorkspaceRequest,
    activate_run_workspace,
)


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


@pytest.fixture(autouse=True)
def _fixed_umask():
    previous = os.umask(0o022)
    yield
    os.umask(previous)


def _participant(agent_id: str) -> dict:
    return {
        "case_id": "case-1",
        "agent_id": agent_id,
        "role": "responder",
        "status": "requested",
    }


def test_collaboration_participants_ledger_is_private_and_tightens(tmp_path: Path) -> None:
    store = CollaborationStore(tmp_path / "collab")
    store.add_participant(_participant("agent-1"))

    path = store.participants_dir / "case-1.jsonl"
    assert _mode(path) == 0o600
    assert _mode(store.participants_dir) == 0o700

    os.chmod(path, 0o644)
    before = path.read_bytes()
    store.add_participant(_participant("agent-2"))
    assert path.read_bytes().startswith(before)
    assert _mode(path) == 0o600
    assert _mode(store.participants_dir) == 0o700


def _workspace_request(run_id: str, request_id: str) -> EnsureRunWorkspaceRequest:
    return EnsureRunWorkspaceRequest(
        home=str(Path("/tmp")),
        template="demo",
        task_name="demo",
        user_prompt="做一个示例任务",
        request_id=request_id,
        run_id=run_id,
        task_id="task-1",
    )


def test_run_workspace_bookkeeping_is_private_and_tightens(tmp_path: Path) -> None:
    root = tmp_path / "tasks" / "2026-10-03" / "demo"
    paths = activate_run_workspace(root, _workspace_request("run-1", "req-1"))

    assert _mode(paths.timeline_jsonl) == 0o600
    assert _mode(paths.state_json) == 0o600
    assert _mode(paths.workspace_json) == 0o600
    assert _mode(paths.task_yaml) == 0o600
    assert _mode(paths.collab_blackboard_md) == 0o600
    assert _mode(paths.collab_messages_jsonl) == 0o600
    assert _mode(paths.work_dir) == 0o700

    # 预置宽权限后追加一条新时间线：文件收紧到 0600，旧内容逐字节保留。
    os.chmod(paths.timeline_jsonl, 0o644)
    before = paths.timeline_jsonl.read_bytes()
    activate_run_workspace(root, _workspace_request("run-2", "req-2"))
    assert paths.timeline_jsonl.read_bytes().startswith(before)
    assert _mode(paths.timeline_jsonl) == 0o600
    assert _mode(paths.work_dir) == 0o700
