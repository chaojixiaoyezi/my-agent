"""wake_intent bounded reaper 测试（#233 生产场景定稿 seq2481/2484①）。

覆盖：租约过期的 claimed 无 attempt → release 回 pending（可重派）；
有 attempt（执行已开始/副作用可能已发生）→ mark reconciliation 保持
claimed 禁自动重放；租约未过期 / token 不匹配 → 不碰。
"""

from __future__ import annotations

import pytest

from agent_py_agent.agent.runtime_db.repository import RuntimeRepository
from agent_py_agent.agent.wake_reaper import reap_expired_claimed_intents

NOW = 1_700_000_000.0


@pytest.fixture
def repo(tmp_path):
    return RuntimeRepository(tmp_path / "home" / "runtime.db")


def _claim(repo, intent_id, *, lease_seconds=10, at=NOW):
    """pending intent → claimed（claim_and_record 同事务落 dispatch 行）。"""
    repo.create_wake_intent(
        intent_id=intent_id, dedup_key=f"k-{intent_id}", owner_id="local/main",
        source="cron", wake_reason="cron_due", next_wake_at=at - 5, now=at,
    )
    result = repo.claim_and_record_wake_dispatch(
        intent_id,
        lease_owner="gw-1",
        lease_seconds=lease_seconds,
        claim_token=f"t-{intent_id}",
        dispatch_event_id=f"evt-{intent_id}",
        handoff_id=f"handoff-{intent_id}",
        now=at,
    )
    assert result["claimed"] is True
    return result


def test_reaper_releases_claimed_without_attempt(repo):
    """租约过期 + 无 attempt（执行席从未建执行记录）→ release 回 pending。"""
    _claim(repo, "i1", lease_seconds=10, at=NOW)
    counts = reap_expired_claimed_intents(repo, now=NOW + 11)
    assert counts == {"released": 1, "reconciled": 0, "skipped": 0, "errors": 0}
    row = repo.get_wake_intent("i1")
    assert row["status"] == "pending"
    assert row["claim_generation"] == 2  # release 递增代际
    assert row["claim_token"] == ""


def test_reaper_marks_reconciliation_when_attempt_started(repo):
    """dispatch 已带 attempt_id（执行已开始/副作用可能已发生）→ 不回收，
    保持 claimed + reconciliation 标记（禁自动重放）。

    模拟真实场景：执行席已建 attempt（accept 未走完就中断）——dispatch 行
    仍 dispatched 但 attempt_id 已回填。
    """
    import sqlite3

    _claim(repo, "i2", lease_seconds=10, at=NOW)
    with sqlite3.connect(repo.db_path) as conn:
        conn.execute(
            "UPDATE wake_dispatches SET attempt_id = 'attempt-1'"
            " WHERE intent_id = 'i2'"
        )
    counts = reap_expired_claimed_intents(repo, now=NOW + 11)
    assert counts == {"released": 0, "reconciled": 1, "skipped": 0, "errors": 0}
    row = repo.get_wake_intent("i2")
    assert row["status"] == "claimed"  # 不回收
    assert "reaper" in row["last_error_ref"]
    # dispatch 也被标 reconciliation（终态化，防止绕过 active gate 重认领）
    dispatch = repo.wake_dispatches_for_intent("i2")[0]
    assert dispatch["status"] == "reconciliation"


def test_reaper_skips_lease_not_expired(repo):
    """租约未过期 → 不碰（released/reconciled 都 0）。"""
    _claim(repo, "i3", lease_seconds=300, at=NOW)
    counts = reap_expired_claimed_intents(repo, now=NOW + 60)
    assert counts == {"released": 0, "reconciled": 0, "skipped": 0, "errors": 0}
    assert repo.get_wake_intent("i3")["status"] == "claimed"


def test_reaper_skips_lease_not_expired_after_reclaim(repo):
    """回收回 pending 后另一 gateway 重新 claim（新租约）→ 老 reaper 不碰。"""
    _claim(repo, "i4", lease_seconds=10, at=NOW)
    # 第一轮回收：无 attempt → release 回 pending（模拟网关 A 崩溃后恢复）
    first = reap_expired_claimed_intents(repo, now=NOW + 11)
    assert first == {"released": 1, "reconciled": 0, "skipped": 0, "errors": 0}
    # 网关 B 重新 claim（新 token/新代际/新租约）
    repo.claim_wake_intent(
        "i4", lease_owner="gw-2", lease_seconds=300,
        claim_token="t-i4-new", now=NOW + 12,
    )
    counts = reap_expired_claimed_intents(repo, now=NOW + 13)
    assert counts == {"released": 0, "reconciled": 0, "skipped": 0, "errors": 0}
    row = repo.get_wake_intent("i4")
    assert row["status"] == "claimed"  # 新租约未过期，不碰
    assert row["lease_owner"] == "gw-2"
