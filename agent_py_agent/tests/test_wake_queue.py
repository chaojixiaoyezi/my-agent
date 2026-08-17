"""模块用途: wake_queue 唤醒字条的仓储测试(扫描治理第一步, owner 拍板)。

验证"任务自己留的闹钟"契约: 每任务一行待醒字条(upsert 幂等)、pop 只取
到期行(CAS 防双叫)、complete/cancel 清行、stale 查"叫了没人干完"的僵尸
候补。所有操作走 runtime.db 索引查询, 不翻任务目录。
"""
from __future__ import annotations

import time
from pathlib import Path

from agent_py_agent.agent.runtime_db.repository import RuntimeRepository


def _repo(tmp_path: Path) -> RuntimeRepository:
    return RuntimeRepository(tmp_path / "home" / "runtime.db")


def _row(wake: dict) -> dict:
    return {
        "root_task_id": wake["root_task_id"],
        "kind": wake["kind"],
        "next_due_at": wake["next_due_at"],
        "status": wake["status"],
    }


def test_upsert_wake_creates_and_updates_idempotently(tmp_path: Path) -> None:
    """同一任务只保留一行待醒字条: 二次 upsert 更新而非新增。"""
    repo = _repo(tmp_path)
    first = repo.upsert_wake(root_task_id="task-1", next_due_at=100.0, kind="sleep")
    assert first["status"] == "pending"
    second = repo.upsert_wake(root_task_id="task-1", next_due_at=200.0, kind="goal_tick")
    assert second["wake_id"] == first["wake_id"]  # 幂等更新同一行
    assert second["next_due_at"] == 200.0
    assert second["kind"] == "goal_tick"
    # 不同任务各自一行
    other = repo.upsert_wake(root_task_id="task-2", next_due_at=150.0)
    assert other["wake_id"] != first["wake_id"]


def test_pop_due_wakes_only_due_and_cas(tmp_path: Path) -> None:
    """pop 只取到期行; 未到期不取; 已 woke 不重复取(防双叫)。"""
    repo = _repo(tmp_path)
    due = repo.upsert_wake(root_task_id="due-task", next_due_at=100.0)
    repo.upsert_wake(root_task_id="future-task", next_due_at=99999.0)
    popped = repo.pop_due_wakes(now=150.0, limit=16)
    assert [w["root_task_id"] for w in popped] == ["due-task"]
    assert popped[0]["status"] == "woke"
    # 再 pop: 已 woke 不再返回
    assert repo.pop_due_wakes(now=150.0, limit=16) == []
    # 未来行不受影响
    future = repo.pop_due_wakes(now=200.0, limit=16)
    assert future == []


def test_pop_due_wakes_respects_limit_and_order(tmp_path: Path) -> None:
    """pop 按 next_due_at 顺序、limit 截断。"""
    repo = _repo(tmp_path)
    for i in range(5):
        repo.upsert_wake(root_task_id=f"task-{i}", next_due_at=float(i))
    popped = repo.pop_due_wakes(now=100.0, limit=3)
    assert [w["root_task_id"] for w in popped] == ["task-0", "task-1", "task-2"]
    rest = repo.pop_due_wakes(now=100.0, limit=16)
    assert [w["root_task_id"] for w in rest] == ["task-3", "task-4"]


def test_complete_wake_removes_row(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    wake = repo.upsert_wake(root_task_id="task-1", next_due_at=100.0)
    assert repo.complete_wake(wake["wake_id"]) is True
    assert repo.complete_wake(wake["wake_id"]) is False  # 幂等删除


def test_cancel_wakes_for_task_clears_all(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    repo.upsert_wake(root_task_id="task-1", next_due_at=100.0)
    # 二次 upsert 同任务仍一行; 不同任务一行
    repo.upsert_wake(root_task_id="task-1", next_due_at=200.0)
    repo.upsert_wake(root_task_id="task-2", next_due_at=300.0)
    assert repo.cancel_wakes_for_task("task-1") == 1
    popped = repo.pop_due_wakes(now=999.0, limit=16)
    assert [w["root_task_id"] for w in popped] == ["task-2"]  # task-1 已清


def test_stale_wakes_detect_uncompleted(tmp_path: Path) -> None:
    """woke 超过超时未 complete = 僵尸候补; 正常完成的不会出现。"""
    repo = _repo(tmp_path)
    repo.upsert_wake(root_task_id="zombie", next_due_at=50.0)
    repo.pop_due_wakes(now=60.0, limit=16)  # 标 woke
    stale = repo.stale_wakes(now=300.0, woke_timeout_seconds=120.0)
    assert [w["root_task_id"] for w in stale] == ["zombie"]
    # 未超时窗口内不判 stale
    assert repo.stale_wakes(now=100.0, woke_timeout_seconds=120.0) == []


def test_stale_then_complete_clears(tmp_path: Path) -> None:
    """僵尸候补经档案核对后 complete 清行, 不再反复出现。"""
    repo = _repo(tmp_path)
    wake = repo.upsert_wake(root_task_id="task-done", next_due_at=50.0)
    repo.pop_due_wakes(now=60.0, limit=16)
    assert repo.stale_wakes(now=300.0, woke_timeout_seconds=120.0)
    repo.complete_wake(wake["wake_id"])
    assert repo.stale_wakes(now=300.0, woke_timeout_seconds=120.0) == []
