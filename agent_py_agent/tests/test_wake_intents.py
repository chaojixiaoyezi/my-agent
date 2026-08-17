"""wake_intents 状态机 repository 测试（#233 规格 §1-§2）。

覆盖：create+dedup 幂等 / due 派生公式（含 expires_at NULL）/ claim CAS /
handoff（禁回退）/ lease 回收（token 匹配）/ cancel / expire / 状态计数。
"""

from __future__ import annotations

import pytest

from agent_py_agent.agent.runtime_db.repository import RuntimeRepository

NOW = 1_700_000_000.0


@pytest.fixture
def repo(tmp_path):
    return RuntimeRepository(tmp_path / "home" / "runtime.db")


def _intent(repo, *, intent_id="intent-1", dedup_key="k1", next_wake_at=NOW - 1,
            source="subagent_completed", wake_reason="subagent_completed",
            owner_id="local/main", **kw):
    return repo.create_wake_intent(
        intent_id=intent_id, dedup_key=dedup_key, owner_id=owner_id,
        source=source, wake_reason=wake_reason, next_wake_at=next_wake_at,
        now=NOW, **kw,
    )


def test_create_and_dedup_idempotent(repo):
    r1 = _intent(repo)
    assert r1["created"] is True
    # 同 dedup_key 第二次 → 幂等返回已存在，不覆盖
    r2 = _intent(repo, intent_id="intent-2")
    assert r2["created"] is False
    assert r2["existing"] is True
    assert r2["intent_id"] == "intent-1"


def test_due_query_only_expired_pending(repo):
    _intent(repo, intent_id="due-1", dedup_key="k-due-1", next_wake_at=NOW - 5)
    _intent(repo, intent_id="future-1", dedup_key="k-future-1", next_wake_at=NOW + 100)
    _intent(repo, intent_id="expired-null", dedup_key="k-null", next_wake_at=NOW - 5, expires_at=None)
    due = repo.due_wake_intents(now=NOW)
    ids = {row["intent_id"] for row in due}
    assert "due-1" in ids
    assert "expired-null" in ids  # expires_at NULL = 无硬过期
    assert "future-1" not in ids  # 未到期
    assert all(row["status"] == "pending" for row in due)


def test_due_excludes_hard_expired(repo):
    _intent(repo, intent_id="expired-hard", dedup_key="k-hard", next_wake_at=NOW - 5,
            expires_at=NOW - 1)
    due = repo.due_wake_intents(now=NOW)
    assert "expired-hard" not in [row["intent_id"] for row in due]


def test_claim_cas_pending_only(repo):
    _intent(repo)
    r = repo.claim_wake_intent("intent-1", lease_owner="gw-1", lease_seconds=300,
                               claim_token="t1", now=NOW)
    assert r["claimed"] is True
    row = repo.get_wake_intent("intent-1")
    assert row["status"] == "claimed"
    assert row["claim_generation"] == 1
    assert row["claim_token"] == "t1"
    assert row["lease_until"] == NOW + 300
    # 二次 claim 拒绝（非 pending——原子条件 status='pending' AND due）
    r2 = repo.claim_wake_intent("intent-1", lease_owner="gw-2", lease_seconds=300,
                                claim_token="t2", now=NOW)
    assert r2["claimed"] is False
    assert r2["reason"] == "not_pending_or_not_due"


def test_handoff_claimed_only_and_no_rollback(repo):
    _intent(repo)
    # 未 claim 直接 handoff → 拒绝
    assert repo.handoff_wake_intent("intent-1", handoff_id="h1", now=NOW)["handed_off"] is False
    repo.claim_wake_intent("intent-1", lease_owner="gw", lease_seconds=300,
                           claim_token="t", now=NOW)
    r = repo.handoff_wake_intent("intent-1", handoff_id="h1", now=NOW)
    assert r["handed_off"] is True
    row = repo.get_wake_intent("intent-1")
    assert row["status"] == "handed_off"
    assert row["handoff_id"] == "h1"
    assert row["handed_off_at"] == NOW
    # handed_off 后 cancel/claim 都拒绝（禁回退）
    assert repo.cancel_wake_intent("intent-1", reason="x", now=NOW)["cancelled"] is False
    assert repo.claim_wake_intent("intent-1", lease_owner="gw", lease_seconds=300,
                                  claim_token="t2", now=NOW)["claimed"] is False


def test_release_lease_requires_expired_and_token_and_generation(repo):
    """群复核门禁（seq2431/2433）：lease_until<=now + token + generation 三条件。"""
    _intent(repo)
    repo.claim_wake_intent("intent-1", lease_owner="gw", lease_seconds=300,
                           claim_token="t1", now=NOW)
    # 租约未过期（lease_until = NOW+300 > now）→ 拒绝释放
    assert repo.release_wake_intent_lease(
        "intent-1", claim_token="t1", expected_generation=1, now=NOW
    )["released"] is False
    # token 不匹配 → 拒绝
    assert repo.release_wake_intent_lease(
        "intent-1", claim_token="wrong", expected_generation=1, now=NOW + 301
    )["released"] is False
    # generation 不匹配 → 拒绝
    assert repo.release_wake_intent_lease(
        "intent-1", claim_token="t1", expected_generation=9, now=NOW + 301
    )["released"] is False
    # 三条件齐（租约过期 + token + generation）→ 释放成功
    r = repo.release_wake_intent_lease(
        "intent-1", claim_token="t1", expected_generation=1, now=NOW + 301
    )
    assert r["released"] is True
    row = repo.get_wake_intent("intent-1")
    assert row["status"] == "pending"
    assert row["claim_generation"] == 2  # 每次 reclaim 递增
    assert row["claim_token"] == ""


def test_mark_lease_expired_keeps_claimed(repo):
    _intent(repo)
    repo.claim_wake_intent("intent-1", lease_owner="gw", lease_seconds=300,
                           claim_token="t", now=NOW)
    # 租约未过期 → 拒绝标记
    assert repo.mark_wake_intent_lease_expired(
        "intent-1", error_ref="unknown_effect", claim_token="t",
        expected_generation=1, now=NOW
    )["marked"] is False
    # 租约过期 + token + generation → 标记成功，保持 claimed
    r = repo.mark_wake_intent_lease_expired(
        "intent-1", error_ref="unknown_effect", claim_token="t",
        expected_generation=1, now=NOW + 301
    )
    assert r["marked"] is True
    row = repo.get_wake_intent("intent-1")
    assert row["status"] == "claimed"  # 保持 claimed，不隐式重放
    assert row["last_error_ref"] == "unknown_effect"


def test_cancel_pending_and_claimed(repo):
    _intent(repo, intent_id="c1", dedup_key="k-c1")
    assert repo.cancel_wake_intent("c1", reason="user_cancel", now=NOW)["cancelled"] is True
    _intent(repo, intent_id="c2", dedup_key="k-c2")
    repo.claim_wake_intent("c2", lease_owner="gw", lease_seconds=300, claim_token="t", now=NOW)
    assert repo.cancel_wake_intent("c2", reason="policy_invalid", now=NOW)["cancelled"] is True


def test_expire_stale(repo):
    _intent(repo, intent_id="stale-1", dedup_key="k-s1", next_wake_at=NOW - 1000)
    _intent(repo, intent_id="fresh-1", dedup_key="k-s2", next_wake_at=NOW - 10)
    n = repo.expire_stale_wake_intents(grace_seconds=300, now=NOW)
    assert n == 1
    assert repo.get_wake_intent("stale-1")["status"] == "expired"
    assert repo.get_wake_intent("fresh-1")["status"] == "pending"


def test_status_counts(repo):
    _intent(repo, intent_id="a", dedup_key="k-a")
    _intent(repo, intent_id="b", dedup_key="k-b")
    repo.claim_wake_intent("b", lease_owner="gw", lease_seconds=300, claim_token="t", now=NOW)
    counts = repo.wake_intent_status_counts()
    assert counts == {"pending": 1, "claimed": 1}
