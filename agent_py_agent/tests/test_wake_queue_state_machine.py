"""WK-INT 回归：wake_queue 持久化状态机（一次性闹钟 → 可恢复待办）。

一次性闹钟的缺陷：pop 即删，执行失败/进程崩溃/429 限流时 wake 永远丢失
（父代理再也不会被叫醒）。升级：pending → claimed(带 lease) → done/failed；
失败 release 回 pending 按 retry_after 退避重试；lease 过期 reclaim 回收。
"""

from __future__ import annotations

import time

from agent_py_agent.agent.runtime_db.repository import RuntimeRepository


def _repo(tmp_path) -> RuntimeRepository:
    return RuntimeRepository(tmp_path / "rt.db")


def test_claim_marks_claimed_with_lease(tmp_path):
    repo = _repo(tmp_path)
    repo.upsert_wake(root_task_id="task-1", next_due_at=0.0, kind="sleep")
    popped = repo.pop_due_wakes(now=10.0, limit=10)
    assert len(popped) == 1
    row = popped[0]
    assert row["status"] == "claimed"
    assert row["lease_until"] >= 10.0
    # 二次 claim 不再选中（CAS 防双叫）
    assert repo.pop_due_wakes(now=10.0, limit=10) == []
    # pending 列表为空（已被 claim）
    assert repo.list_pending_wakes() == []


def test_release_returns_to_pending_with_backoff(tmp_path):
    repo = _repo(tmp_path)
    repo.upsert_wake(root_task_id="task-1", next_due_at=0.0, kind="sleep")
    popped = repo.pop_due_wakes(now=10.0, limit=10)
    wake_id = popped[0]["wake_id"]
    released = repo.release_wake(wake_id, retry_after=15.0, last_error="boom")
    assert released is True
    pending = repo.list_pending_wakes()
    assert len(pending) == 1
    assert pending[0]["attempt_count"] == 1
    assert pending[0]["last_error"] == "boom"
    # retry_after 未到不 claim
    assert repo.pop_due_wakes(now=12.0, limit=10) == []
    # 到达重试点重新 claim
    again = repo.pop_due_wakes(now=15.0, limit=10)
    assert len(again) == 1
    assert again[0]["attempt_count"] == 1


def test_reclaim_expired_lease_returns_to_pending(tmp_path):
    repo = _repo(tmp_path)
    repo.upsert_wake(root_task_id="task-1", next_due_at=0.0, kind="sleep")
    popped = repo.pop_due_wakes(now=10.0, limit=10)
    wake_id = popped[0]["wake_id"]
    # lease 未过期不回收
    assert repo.reclaim_expired_wakes(now=10.0) == 0
    # lease 过期（300s 执行窗）回收
    assert repo.reclaim_expired_wakes(now=10.0 + 301.0) == 1
    pending = repo.list_pending_wakes()
    assert len(pending) == 1
    assert pending[0]["attempt_count"] == 1
    assert pending[0]["last_error"] == "lease_expired_reclaim"
    # 回收后可重新 claim（崩溃恢复闭环）
    assert len(repo.pop_due_wakes(now=10.0 + 302.0, limit=10)) == 1


def test_complete_removes_row(tmp_path):
    repo = _repo(tmp_path)
    repo.upsert_wake(root_task_id="task-1", next_due_at=0.0, kind="sleep")
    popped = repo.pop_due_wakes(now=1.0, limit=10)
    assert repo.complete_wake(popped[0]["wake_id"]) is True
    assert repo.list_pending_wakes() == []


def test_retry_after_frozen_skips_claim(tmp_path):
    """429 冻结语义：retry_after 推到恢复点，冻结期间不 claim 也不丢。"""
    repo = _repo(tmp_path)
    repo.upsert_wake(root_task_id="task-1", next_due_at=0.0, kind="sleep")
    popped = repo.pop_due_wakes(now=1.0, limit=10)
    repo.release_wake(popped[0]["wake_id"], retry_after=100.0, last_error="429")
    assert repo.pop_due_wakes(now=50.0, limit=10) == []
    assert repo.pop_due_wakes(now=100.0, limit=10) != []
