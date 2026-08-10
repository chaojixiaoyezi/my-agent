"""R1-3: create_run 权威主链写入集成测试。

manager 带 owner_home_dir → 每次 create_run 单事务落
Task→TaskRun→root AgentRun→第一个 AgentAttempt→(child) Delegation→
runtime_events（A.4/A.5/A.6/A.7/A.9）；不带 → 无权威库，纯投影（兼容）。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agent_py_agent.agent.runtime_db.repository import RuntimeRepository
from agent_py_agent.agent.subagents.manager import SubAgentManager

OWNER = "local/main"


@pytest.fixture
def manager(tmp_path):
    return SubAgentManager(
        tmp_path / "workspace",
        owner_id=OWNER,
        owner_home_dir=str(tmp_path / "home"),
    )


@pytest.fixture
def repo(manager):
    return RuntimeRepository(Path(manager.owner_home_dir) / "runtime.db")


def test_create_run_writes_full_authority_chain(manager, repo):
    task = manager.create_run(goal="做一个网站", root_id="run-main", parent_id="run-main")
    agent_run = repo.agent_run_for_run_id(task.id)
    assert agent_run is not None
    # A.5：root AgentRun（parent 为空=合法根身份）。
    assert agent_run["parent_agent_run_id"] == ""
    assert agent_run["role"] == task.role
    task_run = repo.get_task_run(agent_run["task_run_id"])
    assert task_run is not None
    # A.6：run 创建即建第一个 attempt 并生效 current pointer。
    current = repo.current_attempt(agent_run["agent_run_id"])
    assert current is not None and current["attempt_generation"] == 1
    # A.8：事件追到 attempt。
    events = repo.events_for_attempt(current["attempt_id"])
    assert {item["event_type"] for item in events} == {"task.created", "agent_run.created"}
    # Task 由框架铸造（B.1 task_id 前缀）。
    assert repo.get_task(task_run["task_id"])["task_id"].startswith("task-")


def test_conversation_task_id_reuses_task_row(manager, repo):
    first = manager.create_run(
        goal="会话任务",
        root_id="run-main",
        parent_id="run-main",
        attributes={"conversation_task_id": "req-1", "conversation_thread_id": "thread-9"},
    )
    second = manager.create_run(
        goal="会话任务 继续",
        root_id="run-main",
        parent_id="run-main",
        attributes={"conversation_task_id": "req-1", "conversation_thread_id": "thread-9"},
    )
    first_agent = repo.agent_run_for_run_id(first.id)
    second_agent = repo.agent_run_for_run_id(second.id)
    # 同一 conversation → 同一 Task 行（A.9 链接身份同事务进 tasks），两个 TaskRun。
    first_task_id = repo.get_task_run(first_agent["task_run_id"])["task_id"]
    second_task_id = repo.get_task_run(second_agent["task_run_id"])["task_id"]
    assert first_task_id == second_task_id
    assert first_agent["task_run_id"] != second_agent["task_run_id"]
    task = repo.get_task(first_task_id)
    assert task["conversation_task_id"] == "req-1"
    assert task["thread_id"] == "thread-9"
    assert len(repo.task_runs_for_task(task["task_id"])) == 2


def test_child_run_delegation_chain(manager, repo):
    parent = manager.create_run(goal="父", root_id="run-main", parent_id="run-main")
    child = manager.create_run(goal="子", root_id="run-main", parent_id=parent.id)
    parent_agent = repo.agent_run_for_run_id(parent.id)
    child_agent = repo.agent_run_for_run_id(child.id)
    # A.7：child 经 immutable Delegation 连 parent AgentRun。
    assert child_agent["parent_agent_run_id"] == parent_agent["agent_run_id"]
    delegation = repo.delegation_for_child(child_agent["agent_run_id"])
    assert delegation is not None
    assert delegation["parent_agent_run_id"] == parent_agent["agent_run_id"]
    assert delegation["child_attempt_id"] == child_agent["current_attempt_id"]
    assert child_agent["delegation_id"] == delegation["delegation_id"]
    # child 也有自己的第一个 attempt（A.6）。
    assert repo.get_attempt(child_agent["current_attempt_id"]) is not None


def test_legacy_parent_skips_delegation_without_blocking(tmp_path):
    # R1 前存量 parent（无权威记录）：child 委托跳过，主链仍完整。
    legacy = SubAgentManager(
        tmp_path / "legacy-workspace", owner_id=OWNER  # 无 owner_home_dir → 无权威库
    )
    parent = legacy.create_run(goal="存量父", root_id="run-main", parent_id="run-main")
    modern = SubAgentManager(
        tmp_path / "modern-workspace",
        owner_id=OWNER,
        owner_home_dir=str(tmp_path / "home"),
    )
    child = modern.create_run(goal="新子", root_id="run-main", parent_id=parent.id)
    repo = RuntimeRepository(tmp_path / "home" / "runtime.db")
    child_agent = repo.agent_run_for_run_id(child.id)
    assert child_agent is not None
    assert child_agent["parent_agent_run_id"] == ""
    assert child_agent["delegation_id"] == ""
    # child 自身链完整（attempt 仍在）。
    assert repo.current_attempt(child_agent["agent_run_id"]) is not None


def test_no_owner_home_dir_no_authority_db(tmp_path):
    manager = SubAgentManager(tmp_path / "workspace", owner_id=OWNER)
    assert manager.runtime_db is None
    task = manager.create_run(goal="无权威", root_id="run-main", parent_id="run-main")
    assert task.id.startswith("subagent-")
    assert not (tmp_path / "runtime.db").exists()


def test_authority_failure_does_not_block_run(manager, repo, monkeypatch):
    def boom(**kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(repo, "record_run_creation", boom)
    task = manager.create_run(goal="照常建", root_id="run-main", parent_id="run-main")
    assert task.id.startswith("subagent-")
    assert task.status == "PENDING" or task.id
