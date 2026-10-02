"""C12c 合同单测：发现层也能补关"根本没有会话任务"的执行总账（D3 崩溃窗口）。

来源：D3 修复（27b8228a4）让运行时收口边在关联文件确实不存在时按代理树关 TaskRun，但发现层的崩溃重放只认可读的
终态关联，分不出"没有关联"和"关联读坏"，于是主执行轮已收口、TaskRun 还没关时进程崩溃，这条总账就没人补关；
step16v 之前留下的同形历史行也一样（10-01 只读试算：生产各 owner 共 338 条）。2026-10-01 在隔离 Gateway 上用测试侧
暂停钩子停在收口边、SIGKILL 复现了崩溃现场（证据 ~/.my-agent/decision-evidence/c12-observations-20261001/）。

复现方法:
    bash ~/.my-agent/releases/claude-tools/3a-scripts/run_files312.sh <worktree> <basetemp> \
        agent_py_agent/tests/test_owner_wake_discovery_task_run_no_link.py
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agent_py_agent.agent.owner_wake_discovery import unfinished_task_ids
from agent_py_agent.agent.runtime_db.repository import RuntimeRepository


@pytest.fixture
def home(tmp_path: Path) -> Path:
    return tmp_path / "home"


def _links(home: Path) -> Path:
    folder = home / "workspace" / "runtime" / "workspaces" / "main-x" / "conversations" / "tasks"
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def _request_run(repo: RuntimeRepository, task_id: str, *, settle: bool = True) -> dict[str, str]:
    """按主链写入登记一次请求的根执行轮；settle=True 时根已 done（崩溃前已收口的那一步）。"""
    created = repo.record_run_creation(
        owner_id="local/main", goal="g", conversation_task_id=task_id, run_id=task_id, role="main",
    )
    if settle:
        repo.settle_agent_run(agent_run_id=created["agent_run_id"], status="done", attempt_id=created["attempt_id"])
    return created


def _task_run(repo: RuntimeRepository, task_run_id: str):
    return repo._runtime_connect().execute(
        "SELECT * FROM task_runs WHERE task_run_id = ?", (task_run_id,),
    ).fetchone()


def _closed_events(repo: RuntimeRepository, task_run_id: str) -> list[dict]:
    rows = repo._runtime_connect().execute(
        "SELECT payload_json FROM runtime_events WHERE task_run_id = ? AND event_type = 'task_run.closed'",
        (task_run_id,),
    ).fetchall()
    return [json.loads(row["payload_json"]) for row in rows]


def test_absent_link_with_terminal_tree_is_closed_by_discovery(home):
    repo = RuntimeRepository(home / "runtime.db")
    created = _request_run(repo, "gwreq-crashed")
    # 同一 owner 下另有别的任务关联，扫描本身不是空目录。
    (_links(home) / "other-task.json").write_text(json.dumps({"task_id": "other-task", "status": "active"}))

    # 第二遍幂等：已关的不再写事件。
    assert unfinished_task_ids(home) == []
    assert unfinished_task_ids(home) == []

    row = _task_run(repo, created["task_run_id"])
    assert row["status"] == "done" and row["closed_at"] > 0
    [closed] = _closed_events(repo, created["task_run_id"])
    assert closed["operator"] == "wake-discovery-task-run-reconcile"
    assert closed["reason"] == "no_conversation_task"


def test_owner_without_any_conversation_store_is_also_absent(home):
    repo = RuntimeRepository(home / "runtime.db")
    created = _request_run(repo, "gwreq-cli")

    unfinished_task_ids(home)

    assert _task_run(repo, created["task_run_id"])["closed_at"] > 0


def test_unreadable_link_file_keeps_task_run_open(home):
    """文件在、读不出：分不出是不是终态关联，保持打开（与运行时收口边同一规则）。"""
    repo = RuntimeRepository(home / "runtime.db")
    created = _request_run(repo, "gwreq-broken")
    (_links(home) / "gwreq-broken.json").write_text("{broken", encoding="utf-8")

    unfinished_task_ids(home)

    assert _task_run(repo, created["task_run_id"])["closed_at"] == 0
    assert _closed_events(repo, created["task_run_id"]) == []


def test_active_link_between_turns_keeps_task_run_open(home):
    repo = RuntimeRepository(home / "runtime.db")
    created = _request_run(repo, "task-persistent")
    (_links(home) / "task-persistent.json").write_text(json.dumps({"task_id": "task-persistent", "status": "active"}))

    unfinished_task_ids(home)

    assert _task_run(repo, created["task_run_id"])["closed_at"] == 0


def test_running_root_without_link_keeps_task_run_open(home):
    repo = RuntimeRepository(home / "runtime.db")
    created = _request_run(repo, "gwreq-running", settle=False)

    unfinished_task_ids(home)

    assert _task_run(repo, created["task_run_id"])["closed_at"] == 0


def test_incomplete_link_scan_keeps_task_run_open(home, monkeypatch):
    """列不全关联目录时不能把"没扫到"当成"没有关联"。"""
    repo = RuntimeRepository(home / "runtime.db")
    created = _request_run(repo, "gwreq-scan")
    _links(home)
    original = Path.glob

    def flaky_glob(self, pattern, *args, **kwargs):
        if pattern == "*/conversations/tasks":
            raise OSError("scan failed")
        return original(self, pattern, *args, **kwargs)

    monkeypatch.setattr(Path, "glob", flaky_glob)

    unfinished_task_ids(home)

    assert _task_run(repo, created["task_run_id"])["closed_at"] == 0
