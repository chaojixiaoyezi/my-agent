"""R1-3: create_run 权威主链写入集成测试。

manager 带 owner_home_dir → 每次 create_run 单事务落
Task→TaskRun→root AgentRun→第一个 AgentAttempt→(child) Delegation→
runtime_events（A.4/A.5/A.6/A.7/A.9）；不带 → 无权威库，纯投影（兼容）。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from agent_py_agent.agent.runtime_db.repository import (
    RuntimeConflictError,
    RuntimeRepository,
)
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
    # A.6：run 创建即登记第一个 pending attempt 并生效 current pointer。
    current = repo.current_attempt(agent_run["agent_run_id"])
    assert current is not None and current["attempt_generation"] == 1
    assert current["status"] == "pending"
    assert "runner_pid" not in json.loads(current["metadata_json"])
    # A.8：事件追到 attempt。
    events = repo.events_for_attempt(current["attempt_id"])
    assert {item["event_type"] for item in events} == {"task.created", "agent_run.created"}
    # Task 由框架铸造（B.1 task_id 前缀）。
    assert repo.get_task(task_run["task_id"])["task_id"].startswith("task-")


def test_new_runtime_schema_omits_retired_task_and_acceptance_state(repo):
    """Task 只保存长期身份；新库不再创建已退休的机器验收状态面。"""
    with repo._runtime_connection() as conn:
        tables = {
            str(row["name"])
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
        task_columns = {
            str(row["name"]) for row in conn.execute("PRAGMA table_info(tasks)")
        }
        task_run_columns = {
            str(row["name"]) for row in conn.execute("PRAGMA table_info(task_runs)")
        }

    assert "status" not in task_columns
    assert "current_contract_id" not in task_run_columns
    assert "acceptance_contracts" not in tables
    assert "validator_operations" not in tables
    assert not hasattr(repo, "closeout_task_run")


def test_agent_thread_can_be_cold_imported_before_composition_root():
    """叶子模块先导入时，agent_core 包初始化不得提前装载整套运行时。"""
    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "from agent_py_agent.agent.conversation.agent_thread "
                "import ensure_subagent_thread; "
                "from agent_py_agent.agent.core import SimpleAgent; "
                "assert ensure_subagent_thread and SimpleAgent"
            ),
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr


def test_first_runner_start_activates_pending_attempt_without_new_generation(manager, repo):
    task = manager.create_run(goal="长任务", root_id="run-main", parent_id="run-main")
    agent_run = repo.agent_run_for_run_id(task.id)
    pending = repo.current_attempt(agent_run["agent_run_id"])

    prepared = manager.lifecycle.prepare_runner_attempt(task.id)
    active = repo.current_attempt(agent_run["agent_run_id"])

    assert prepared.runner_active_attempt_id == pending["attempt_id"]
    assert active["attempt_id"] == pending["attempt_id"]
    assert active["attempt_generation"] == 1
    assert active["status"] == "running"
    assert json.loads(active["metadata_json"])["runner_pid"] == os.getpid()
    assert len(repo.attempts_for_run(agent_run["agent_run_id"])) == 1
    assert repo.has_active_exec_lock(agent_run["agent_run_id"])
    assert [
        event["event_type"] for event in repo.events_for_attempt(active["attempt_id"])
    ] == [
        "task.created",
        "agent_run.created",
        "agent_attempt.started",
        "agent_run.started",
    ]


def test_duplicate_runner_start_rejects_current_running_attempt(manager, repo):
    task = manager.create_run(goal="长任务", root_id="run-main", parent_id="run-main")
    first = manager.lifecycle.prepare_runner_attempt(task.id)

    with pytest.raises(RuntimeConflictError, match="仍在运行"):
        manager.lifecycle.prepare_runner_attempt(task.id)

    agent_run = repo.agent_run_for_run_id(task.id)
    attempts = repo.attempts_for_run(agent_run["agent_run_id"])
    assert [attempt["attempt_id"] for attempt in attempts] == [
        first.runner_active_attempt_id
    ]


def test_explicit_input_queues_one_pending_successor_before_runner_activation(
    manager,
    repo,
):
    task = manager.create_run(goal="等待用户补充", root_id="run-main", parent_id="run-main")
    first = manager.lifecycle.prepare_runner_attempt(task.id)
    agent_run = repo.agent_run_for_run_id(task.id)
    assert agent_run is not None
    assert repo.settle_agent_attempt(
        agent_run_id=str(agent_run["agent_run_id"]),
        attempt_id=first.runner_active_attempt_id,
    )["settled"] is True
    projected = manager.load(task.id)
    projected.status = "PENDING"
    projected.runner_active_attempt_id = ""
    manager.save(projected)

    queued = repo.queue_pending_attempt(
        str(agent_run["agent_run_id"]),
        source="user_agent_guidance",
    )
    replay = repo.queue_pending_attempt(
        str(agent_run["agent_run_id"]),
        source="user_agent_guidance",
    )

    assert queued["attempt_id"] == replay["attempt_id"]
    assert queued["attempt_generation"] == 2
    assert queued["status"] == "pending"
    assert repo.has_active_exec_lock(str(agent_run["agent_run_id"])) is False
    prepared = manager.lifecycle.prepare_runner_attempt(task.id)
    assert prepared.runner_active_attempt_id == queued["attempt_id"]
    assert repo.current_attempt(str(agent_run["agent_run_id"]))["status"] == "running"
    assert len(repo.attempts_for_run(str(agent_run["agent_run_id"]))) == 2


def test_concurrent_explicit_inputs_reuse_one_pending_successor(manager, repo):
    task = manager.create_run(goal="并发插话", root_id="run-main", parent_id="run-main")
    first = manager.lifecycle.prepare_runner_attempt(task.id)
    agent_run = repo.agent_run_for_run_id(task.id)
    assert agent_run is not None
    assert repo.settle_agent_attempt(
        agent_run_id=str(agent_run["agent_run_id"]),
        attempt_id=first.runner_active_attempt_id,
    )["settled"] is True
    barrier = threading.Barrier(3)
    queued_ids: list[str] = []
    failures: list[BaseException] = []

    def queue() -> None:
        try:
            barrier.wait(timeout=2.0)
            queued = repo.queue_pending_attempt(
                str(agent_run["agent_run_id"]),
                source="user_agent_guidance",
            )
            queued_ids.append(str(queued["attempt_id"]))
        except BaseException as exc:  # pragma: no cover - asserted below
            failures.append(exc)

    workers = [threading.Thread(target=queue) for _ in range(2)]
    for worker in workers:
        worker.start()
    barrier.wait(timeout=2.0)
    for worker in workers:
        worker.join(timeout=2.0)

    assert failures == []
    assert len(queued_ids) == 2
    assert len(set(queued_ids)) == 1
    assert len(repo.attempts_for_run(str(agent_run["agent_run_id"]))) == 2


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
    # F9（A.4）：child 并入 parent 的 TaskRun——一次 TaskRun 下一棵 root/child
    # 树，不跨 TaskRun 引用；child 共享 parent 的 Task 身份（无孤儿 Task 行）。
    assert child_agent["task_run_id"] == parent_agent["task_run_id"]
    assert len(repo.agent_runs_for_task_run(parent_agent["task_run_id"])) == 2
    child_task_run = repo.get_task_run(child_agent["task_run_id"])
    parent_task_run = repo.get_task_run(parent_agent["task_run_id"])
    assert child_task_run["task_id"] == parent_task_run["task_id"]


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


def test_authority_write_failure_fails_closed(manager, monkeypatch):
    """F8：有 home 时权威写入失败不再静默降级——缺记录的 run 会被授权门
    按"权威记录缺失"拒绝（B.6），成为不可管理的幻影，故 create 抛错。"""
    def boom(**kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(manager.runtime_db, "record_run_creation", boom)
    with pytest.raises(OSError, match="disk full"):
        manager.create_run(goal="照常建", root_id="run-main", parent_id="run-main")


def test_missing_repo_with_home_fails_closed(tmp_path):
    """F8：attach 静默降级（home 在但 runtime_db=None）→ create 拒绝，不产生幻影 run。"""
    manager = SubAgentManager(
        tmp_path / "workspace",
        owner_id=OWNER,
        owner_home_dir=str(tmp_path / "home"),
    )
    manager.runtime_db = None  # 模拟 _attach_runtime_db 的 OSError 静默降级
    with pytest.raises(PermissionError, match="权威库不可用"):
        manager.create_run(goal="无权威", root_id="run-main", parent_id="run-main")
    # 文件层也没有残留任务（_finalize_task 未执行）。
    assert not (tmp_path / "workspace").exists() or not list((tmp_path / "workspace").rglob("task.json"))
