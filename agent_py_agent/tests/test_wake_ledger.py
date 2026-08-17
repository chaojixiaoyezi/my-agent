"""wake_dispatch ledger + canonical policy/circuit 测试（#233 / seq2455-2458 硬合同）。

覆盖：
- wake_policies：唯一 canonical 授权源（无记录/代际不匹配/撤销/来源白名单/
  provider scope 不匹配 → fail-closed）；generation 更新与 revoked 原子化。
- provider_circuits：冻结投影（frozen + retry_after + recover）。
- wake_dispatches：claim+outbox 同事务、acceptance CAS（lease 未过期 +
  handoff 匹配）、幂等重放、crash-after-claim、lease 过期迟到 receipt、
  双 Gateway 竞态、active_attempt_id 门。
"""

from __future__ import annotations

import pytest

from agent_py_agent.agent.runtime_db.repository import RuntimeRepository

NOW = 1_700_000_000.0


@pytest.fixture
def repo(tmp_path):
    return RuntimeRepository(tmp_path / "home" / "runtime.db")


def _intent(repo, intent_id="intent-1", dedup_key="k1", *, source="cron",
            wake_reason="cron_due", policy="async", generation=1,
            scope="opencode", **kw):
    kw.setdefault("next_wake_at", NOW - 1)
    return repo.create_wake_intent(
        intent_id=intent_id, dedup_key=dedup_key, owner_id="local/main",
        source=source, wake_reason=wake_reason,
        continuation_policy=policy, policy_generation=generation,
        provider_scope_ref=scope, now=NOW, **kw,
    )


def _claim(repo, intent_id="intent-1", owner="gw-1", *, now=NOW):
    return repo.claim_and_record_wake_dispatch(
        intent_id, lease_owner=owner, lease_seconds=300,
        claim_token=f"{owner}:t{int(now * 1000)}",
        dispatch_event_id=f"ev-{intent_id}-{int(now * 1000)}",
        handoff_id=f"hd-{intent_id}-{int(now * 1000)}",
        now=now,
    )


# ---------------------------------------------------------- wake_policies
def test_policy_allows_when_current_matches(repo):
    repo.set_wake_policy(
        policy_id="pol-1", owner_id="local/main", continuation_policy="async",
        policy_generation=1, allowed_sources="cron,heartbeat",
        provider_scope_ref="opencode", now=NOW,
    )
    assert repo.wake_policy_allows({"owner_id": "local/main",
                                    "continuation_policy": "async",
                                    "policy_generation": 1,
                                    "source": "cron",
                                    "provider_scope_ref": "opencode"}) is True


def test_policy_missing_record_rejected(repo):
    assert repo.wake_policy_allows({"owner_id": "local/main",
                                    "continuation_policy": "async",
                                    "policy_generation": 1,
                                    "source": "cron",
                                    "provider_scope_ref": "opencode"}) is False


def test_policy_generation_mismatch_rejected(repo):
    repo.set_wake_policy(
        policy_id="pol-1", owner_id="local/main", continuation_policy="async",
        policy_generation=2, allowed_sources="cron", provider_scope_ref="opencode",
        now=NOW,
    )
    # intent 声明 generation=1，ledger 当前是 2 → 拒绝
    assert repo.wake_policy_allows({"owner_id": "local/main",
                                    "continuation_policy": "async",
                                    "policy_generation": 1,
                                    "source": "cron",
                                    "provider_scope_ref": "opencode"}) is False


def test_policy_revoked_rejected(repo):
    repo.set_wake_policy(
        policy_id="pol-1", owner_id="local/main", continuation_policy="async",
        policy_generation=1, allowed_sources="cron", provider_scope_ref="opencode",
        now=NOW,
    )
    repo.revoke_wake_policy(owner_id="local/main", continuation_policy="async", now=NOW)
    assert repo.wake_policy_allows({"owner_id": "local/main",
                                    "continuation_policy": "async",
                                    "policy_generation": 1,
                                    "source": "cron",
                                    "provider_scope_ref": "opencode"}) is False


def test_policy_source_not_in_allowed_rejected(repo):
    repo.set_wake_policy(
        policy_id="pol-1", owner_id="local/main", continuation_policy="async",
        policy_generation=1, allowed_sources="cron", provider_scope_ref="opencode",
        now=NOW,
    )
    assert repo.wake_policy_allows({"owner_id": "local/main",
                                    "continuation_policy": "async",
                                    "policy_generation": 1,
                                    "source": "heartbeat",
                                    "provider_scope_ref": "opencode"}) is False
    # allowed_sources 空 = 不设白名单（全放行来源），但 scope 仍校验
    repo.set_wake_policy(
        policy_id="pol-2", owner_id="local/main", continuation_policy="async",
        policy_generation=2, allowed_sources="", provider_scope_ref="opencode",
        now=NOW + 1,
    )
    assert repo.wake_policy_allows({"owner_id": "local/main",
                                    "continuation_policy": "async",
                                    "policy_generation": 2,
                                    "source": "heartbeat",
                                    "provider_scope_ref": "opencode"}) is True


def test_policy_provider_scope_mismatch_rejected(repo):
    repo.set_wake_policy(
        policy_id="pol-1", owner_id="local/main", continuation_policy="async",
        policy_generation=1, allowed_sources="cron", provider_scope_ref="opencode",
        now=NOW,
    )
    assert repo.wake_policy_allows({"owner_id": "local/main",
                                    "continuation_policy": "async",
                                    "policy_generation": 1,
                                    "source": "cron",
                                    "provider_scope_ref": "minimax"}) is False


def test_policy_generation_update_atomically_revokes_old(repo):
    repo.set_wake_policy(
        policy_id="pol-1", owner_id="local/main", continuation_policy="async",
        policy_generation=1, allowed_sources="cron", provider_scope_ref="opencode",
        now=NOW,
    )
    repo.set_wake_policy(
        policy_id="pol-2", owner_id="local/main", continuation_policy="async",
        policy_generation=2, allowed_sources="cron", provider_scope_ref="opencode",
        now=NOW + 1,
    )
    cur = repo.current_wake_policy("local/main", "async")
    assert cur["policy_generation"] == 2 and int(cur["revoked_at"]) == 0
    # 旧 generation 拒绝（唯一 canonical，无两套并存）
    assert repo.wake_policy_allows({"owner_id": "local/main",
                                    "continuation_policy": "async",
                                    "policy_generation": 1,
                                    "source": "cron",
                                    "provider_scope_ref": "opencode"}) is False


# ------------------------------------------------------- provider_circuits
def test_circuit_default_not_frozen(repo):
    assert repo.provider_circuit_frozen("opencode", now=NOW) is False


def test_circuit_frozen_with_future_retry_after(repo):
    repo.freeze_provider_circuit("opencode", retry_after=NOW + 600,
                                 reason="quota_429", now=NOW)
    assert repo.provider_circuit_frozen("opencode", now=NOW) is True
    assert repo.provider_circuit_frozen("opencode", now=NOW + 601) is False  # 已过 retry_after


def test_circuit_recover(repo):
    repo.freeze_provider_circuit("opencode", retry_after=NOW + 600,
                                 reason="quota_429", now=NOW)
    repo.recover_provider_circuit("opencode", now=NOW + 1)
    assert repo.provider_circuit_frozen("opencode", now=NOW + 2) is False


# ------------------------------------------------------- wake_dispatches
def test_claim_records_dispatch_same_txn(repo):
    _intent(repo)
    c = _claim(repo)
    assert c["claimed"] and c["dispatch_id"] and c["dispatch_event_id"] and c["handoff_id"]
    row = repo.get_wake_intent("intent-1")
    assert row["status"] == "claimed"
    d = repo.get_wake_dispatch(c["dispatch_id"])
    assert d["status"] == "dispatched"
    assert d["claim_generation"] == c["claim_generation"]
    assert d["lease_until"] == NOW + 300
    # 同一 intent 第二次 claim（双 Gateway 竞态）→ 拒绝（CAS status=pending + active 门）
    c2 = _claim(repo, owner="gw-2")
    assert c2["claimed"] is False
    assert repo.wake_dispatches_for_intent("intent-1")[0]["status"] == "dispatched"


def test_accept_happy_path_cas_handed_off(repo):
    _intent(repo)
    c = _claim(repo)
    r = repo.accept_wake_dispatch(c["dispatch_id"], handoff_id=c["handoff_id"],
                                  attempt_id="attempt-1", now=NOW + 1)
    assert r["accepted"] is True
    d = repo.get_wake_dispatch(c["dispatch_id"])
    assert d["status"] == "accepted" and d["accepted_at"] == NOW + 1
    assert d["attempt_id"] == "attempt-1"
    row = repo.get_wake_intent("intent-1")
    assert row["status"] == "handed_off"
    assert row["handoff_id"] == c["handoff_id"]
    assert row["active_attempt_id"] == "attempt-1"


def test_accept_idempotent_on_duplicate_delivery(repo):
    """重复送达（同 handoff + 同 attempt）→ 幂等成功。"""
    _intent(repo)
    c = _claim(repo)
    r1 = repo.accept_wake_dispatch(c["dispatch_id"], handoff_id=c["handoff_id"],
                                   attempt_id="attempt-1", now=NOW + 1)
    r2 = repo.accept_wake_dispatch(c["dispatch_id"], handoff_id=c["handoff_id"],
                                   attempt_id="attempt-1", now=NOW + 2)
    assert r1["accepted"] is True and r2["accepted"] is True and r2["idempotent"] is True
    # 不同 attempt 的重复投递 → 拒绝（防旧 attempt 抢占）
    r3 = repo.accept_wake_dispatch(c["dispatch_id"], handoff_id=c["handoff_id"],
                                   attempt_id="attempt-2", now=NOW + 3)
    assert r3["accepted"] is False


def test_accept_wrong_handoff_rejected(repo):
    _intent(repo)
    c = _claim(repo)
    r = repo.accept_wake_dispatch(c["dispatch_id"], handoff_id="wrong-handoff",
                                  attempt_id="attempt-1", now=NOW + 1)
    assert r["accepted"] is False and r["reason"] == "handoff_mismatch"
    assert repo.get_wake_intent("intent-1")["status"] == "claimed"


def test_accept_lease_expired_late_receipt_to_reconciliation(repo):
    """lease 已过期的迟到 receipt → 转 reconciliation，不把旧 intent 标 handed_off。"""
    _intent(repo)
    c = _claim(repo)
    r = repo.accept_wake_dispatch(c["dispatch_id"], handoff_id=c["handoff_id"],
                                  attempt_id="attempt-1", now=NOW + 301)  # lease_until=NOW+300 已过期
    assert r["accepted"] is False
    assert r["reason"] == "lease_expired_reconciliation"
    d = repo.get_wake_dispatch(c["dispatch_id"])
    assert d["status"] == "reconciliation"
    row = repo.get_wake_intent("intent-1")
    assert row["status"] == "claimed"  # 未被标 handed_off


def test_accept_intent_generation_mismatch_rejected(repo):
    _intent(repo)
    c = _claim(repo)
    # 模拟 intent 已被 release 回 pending 且换代（generation 变化）→ 迟到 accept 拒绝
    repo.release_wake_intent_lease("intent-1", claim_token=c["claim_token"],
                                   expected_generation=c["claim_generation"], now=NOW + 301)
    repo.release_wake_dispatch(c["dispatch_id"], now=NOW + 301)
    r = repo.accept_wake_dispatch(c["dispatch_id"], handoff_id=c["handoff_id"],
                                  attempt_id="attempt-1", now=NOW + 302)
    assert r["accepted"] is False


def test_claim_blocked_when_active_attempt(repo):
    """同 intent 已有 active attempt → 新 claim 拒绝（active_attempt_id 门）。"""
    _intent(repo)
    c = _claim(repo)
    repo.accept_wake_dispatch(c["dispatch_id"], handoff_id=c["handoff_id"],
                              attempt_id="attempt-1", now=NOW + 1)
    # 已 handed_off + active_attempt_id 非空 → 二次 claim 拒绝
    c2 = _claim(repo, owner="gw-2", now=NOW + 2)
    assert c2["claimed"] is False


def test_release_pair_returns_to_pending(repo):
    """lease 过期 + 无已知副作用 → release dispatch + intent，代际递增防旧认领。"""
    _intent(repo)
    c = _claim(repo)
    r = repo.release_wake_dispatch(c["dispatch_id"], now=NOW + 301)
    assert r["released"] is True
    ri = repo.release_wake_intent_lease("intent-1", claim_token=c["claim_token"],
                                        expected_generation=c["claim_generation"],
                                        now=NOW + 301)
    assert ri["released"] is True
    row = repo.get_wake_intent("intent-1")
    assert row["status"] == "pending"
    assert row["claim_generation"] == c["claim_generation"] + 1
    assert row["claim_token"] == ""
    assert repo.get_wake_dispatch(c["dispatch_id"])["status"] == "released"


def test_release_requires_no_active_attempt(repo):
    """active attempt 未清 → release 拒绝（防未知副作用被误释放）。"""
    _intent(repo)
    c = _claim(repo)
    repo.accept_wake_dispatch(c["dispatch_id"], handoff_id=c["handoff_id"],
                              attempt_id="attempt-1", now=NOW + 1)
    # lease 过期后想 release —— active_attempt_id 非空 → 拒绝
    ri = repo.release_wake_intent_lease("intent-1", claim_token=c["claim_token"],
                                        expected_generation=c["claim_generation"],
                                        now=NOW + 301)
    assert ri["released"] is False


def test_clear_active_attempt_cas(repo):
    _intent(repo)
    c = _claim(repo)
    repo.accept_wake_dispatch(c["dispatch_id"], handoff_id=c["handoff_id"],
                              attempt_id="attempt-1", now=NOW + 1)
    # 错误 attempt 清不掉
    assert repo.clear_wake_intent_active_attempt("intent-1", attempt_id="wrong",
                                                 now=NOW + 2)["cleared"] is False
    assert repo.clear_wake_intent_active_attempt("intent-1", attempt_id="attempt-1",
                                                 now=NOW + 2)["cleared"] is True
    assert repo.get_wake_intent("intent-1")["active_attempt_id"] == ""


def test_crash_after_claim_intent_stays_claimed(repo):
    """crash-after-claim：claim+outbox 已持久但未 accept → intent 保持 claimed +
    dispatch 保持 dispatched，reconciler 可见（不丢、不误标 handed_off）。"""
    _intent(repo)
    c = _claim(repo)
    # 模拟进程死：无 accept、无 release
    row = repo.get_wake_intent("intent-1")
    assert row["status"] == "claimed"
    assert row["active_attempt_id"] == ""
    d = repo.get_wake_dispatch(c["dispatch_id"])
    assert d["status"] == "dispatched"
    # lease 过期后可被 reconciler 回收（先标 reconciliation 再接管）
    r = repo.mark_wake_dispatch_reconciliation(c["dispatch_id"], error_ref="crash_recovered",
                                               now=NOW + 301)
    assert r["marked"] is True


def test_outbox_written_not_delivered(repo):
    """outbox 已写未送达：dispatch 行存在、accept 从未调用 → 状态保持 dispatched。"""
    _intent(repo)
    c = _claim(repo)
    dispatches = repo.wake_dispatches_for_intent("intent-1")
    assert len(dispatches) == 1 and dispatches[0]["status"] == "dispatched"
    assert repo.get_wake_intent("intent-1")["status"] == "claimed"


def test_fail_dispatch_records_error_intent_claimed(repo):
    """执行席拒绝/接收失败：dispatch→failed 记录 error_ref，intent 保持 claimed。"""
    _intent(repo)
    c = _claim(repo)
    r = repo.fail_wake_dispatch(c["dispatch_id"], error_ref="pool_full", now=NOW + 1)
    assert r["failed"] is True
    assert repo.get_wake_dispatch(c["dispatch_id"])["status"] == "failed"
    assert repo.get_wake_intent("intent-1")["status"] == "claimed"
