"""wake_dispatch ledger + canonical policy/circuit 测试（#233 / seq2455-2463 硬合同）。

覆盖：
- wake_policies：唯一 canonical 授权源（partial unique index 保留撤销历史；
  无记录/代际不匹配/撤销/空白名单/空 scope/provider scope 不匹配 → fail-closed；
  scope 层级匹配）。
- provider_circuits：冻结投影（frozen + retry_after + recover）。
- wake_dispatches：claim+outbox 同事务、acceptance CAS（attempt 受控 ref +
  lease 未过期 + handoff 匹配）、幂等重放、crash-after-claim、lease 过期迟到
  receipt、双 Gateway 竞态、active_attempt_id 门、终结 CAS 受控身份。
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
            scope="opencode", task_id="", run_id="", owner_id="local/main", **kw):
    kw.setdefault("next_wake_at", NOW - 1)
    return repo.create_wake_intent(
        intent_id=intent_id, dedup_key=dedup_key, owner_id=owner_id,
        source=source, wake_reason=wake_reason,
        continuation_policy=policy, policy_generation=generation,
        provider_scope_ref=scope, task_id=task_id, run_id=run_id, now=NOW, **kw,
    )


def _claim(repo, intent_id="intent-1", owner="gw-1", *, now=NOW):
    return repo.claim_and_record_wake_dispatch(
        intent_id, lease_owner=owner, lease_seconds=300,
        claim_token=f"{owner}:t{int(now * 1000)}",
        dispatch_event_id=f"ev-{intent_id}-{int(now * 1000)}",
        handoff_id=f"hd-{intent_id}-{int(now * 1000)}",
        now=now,
    )


def _real_attempt(repo):
    """创建 canonical agent_attempts 行（seq2463③：accept 的 attempt_id 必须
    是真实受控 ref）。"""
    task = repo.create_task(owner_id="local/main", title="ledger-test")
    task_run = repo.create_task_run(task_id=task["task_id"])
    agent_run = repo.create_agent_run(task_run_id=task_run["task_run_id"], role="main")
    attempt = repo.create_attempt(agent_run["agent_run_id"])
    return str(attempt["attempt_id"])


# ---------------------------------------------------------- wake_policies
def test_policy_allows_when_current_matches(repo):
    repo.set_wake_policy(
        policy_id="pol-1", owner_id="local/main", continuation_policy="async",
        policy_generation=1, allowed_sources="cron,heartbeat",
        provider_scope_ref="opencode", scope_ref="local/main", now=NOW,
    )
    assert repo.wake_policy_allows({"owner_id": "local/main",
                                    "continuation_policy": "async",
                                    "policy_generation": 1,
                                    "source": "cron",
                                    "provider_scope_ref": "opencode",
                                    "task_id": "", "run_id": ""}) is True


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
        scope_ref="local/main", now=NOW,
    )
    assert repo.wake_policy_allows({"owner_id": "local/main",
                                    "continuation_policy": "async",
                                    "policy_generation": 1,
                                    "source": "cron",
                                    "provider_scope_ref": "opencode"}) is False


def test_policy_revoked_rejected(repo):
    repo.set_wake_policy(
        policy_id="pol-1", owner_id="local/main", continuation_policy="async",
        policy_generation=1, allowed_sources="cron", provider_scope_ref="opencode",
        scope_ref="local/main", now=NOW,
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
        scope_ref="local/main", now=NOW,
    )
    # heartbeat 不在白名单 → 拒绝
    assert repo.wake_policy_allows({"owner_id": "local/main",
                                    "continuation_policy": "async",
                                    "policy_generation": 1,
                                    "source": "heartbeat",
                                    "provider_scope_ref": "opencode",
                                    "task_id": "", "run_id": ""}) is False


def test_policy_empty_allowed_sources_rejected(repo):
    """seq2463②：空白名单不隐式放大授权 → 拒绝（非 wildcard）。"""
    repo.set_wake_policy(
        policy_id="pol-1", owner_id="local/main", continuation_policy="async",
        policy_generation=1, allowed_sources="", provider_scope_ref="opencode",
        scope_ref="local/main", now=NOW,
    )
    assert repo.wake_policy_allows({"owner_id": "local/main",
                                    "continuation_policy": "async",
                                    "policy_generation": 1,
                                    "source": "cron",
                                    "provider_scope_ref": "opencode",
                                    "task_id": "", "run_id": ""}) is False


def test_policy_empty_scope_ref_rejected(repo):
    """seq2463②：空作用域 fail-closed，owner 级策略不得越过作用域。"""
    repo.set_wake_policy(
        policy_id="pol-1", owner_id="local/main", continuation_policy="async",
        policy_generation=1, allowed_sources="cron", provider_scope_ref="opencode",
        scope_ref="", now=NOW,
    )
    assert repo.wake_policy_allows({"owner_id": "local/main",
                                    "continuation_policy": "async",
                                    "policy_generation": 1,
                                    "source": "cron",
                                    "provider_scope_ref": "opencode",
                                    "task_id": "", "run_id": ""}) is False


def test_policy_scope_hierarchy_matches(repo):
    """scope_ref 段前缀匹配：owner / owner:task / owner:task:run；更窄不能授权更宽。"""
    repo.set_wake_policy(
        policy_id="p-owner", owner_id="u1", continuation_policy="async",
        policy_generation=1, allowed_sources="cron", provider_scope_ref="opencode",
        scope_ref="u1", now=NOW,
    )
    # owner 级授权任意 task（task 更具体，owner 前缀覆盖）
    assert repo.wake_policy_allows({"owner_id": "u1", "continuation_policy": "async",
                                    "policy_generation": 1, "source": "cron",
                                    "provider_scope_ref": "opencode",
                                    "task_id": "task-x", "run_id": ""}) is True
    # task 级授权：只允许该 task
    repo.set_wake_policy(
        policy_id="p-task", owner_id="u1", continuation_policy="async",
        policy_generation=2, allowed_sources="cron", provider_scope_ref="opencode",
        scope_ref="u1:task-a", now=NOW + 1,
    )
    assert repo.wake_policy_allows({"owner_id": "u1", "continuation_policy": "async",
                                    "policy_generation": 2, "source": "cron",
                                    "provider_scope_ref": "opencode",
                                    "task_id": "task-a", "run_id": ""}) is True
    assert repo.wake_policy_allows({"owner_id": "u1", "continuation_policy": "async",
                                    "policy_generation": 2, "source": "cron",
                                    "provider_scope_ref": "opencode",
                                    "task_id": "task-b", "run_id": ""}) is False


def test_policy_provider_scope_mismatch_rejected(repo):
    repo.set_wake_policy(
        policy_id="pol-1", owner_id="local/main", continuation_policy="async",
        policy_generation=1, allowed_sources="cron", provider_scope_ref="opencode",
        scope_ref="local/main", now=NOW,
    )
    assert repo.wake_policy_allows({"owner_id": "local/main",
                                    "continuation_policy": "async",
                                    "policy_generation": 1,
                                    "source": "cron",
                                    "provider_scope_ref": "minimax"}) is False


def test_policy_history_retained_and_old_revoked(repo):
    """seq2463①：新 generation 写入保留旧代历史行（revoked 非 0），可审计。"""
    repo.set_wake_policy(
        policy_id="p1", owner_id="local/main", continuation_policy="async",
        policy_generation=1, allowed_sources="cron", provider_scope_ref="opencode",
        scope_ref="local/main", now=NOW,
    )
    repo.set_wake_policy(
        policy_id="p2", owner_id="local/main", continuation_policy="async",
        policy_generation=2, allowed_sources="cron", provider_scope_ref="opencode",
        scope_ref="local/main", now=NOW + 1,
    )
    # 旧代 p1 行仍在且 revoked；新代 p2 是当前
    cur = repo.current_wake_policy("local/main", "async")
    assert cur["policy_id"] == "p2" and cur["policy_generation"] == 2
    assert int(cur["revoked_at"]) == 0
    with repo._runtime_connection() as conn:
        rows = conn.execute(
            "SELECT policy_id, revoked_at FROM wake_policies"
            " WHERE owner_id='local/main' AND continuation_policy='async'"
        ).fetchall()
    ids = {r["policy_id"]: r for r in rows}
    assert "p1" in ids and int(ids["p1"]["revoked_at"]) > 0  # 历史保留 + 已撤销
    # 旧代 gen1 拒绝
    assert repo.wake_policy_allows({"owner_id": "local/main",
                                    "continuation_policy": "async",
                                    "policy_generation": 1,
                                    "source": "cron",
                                    "provider_scope_ref": "opencode",
                                    "task_id": "", "run_id": ""}) is False
    # 全撤销 → current None（fail-closed）
    repo.revoke_wake_policy(owner_id="local/main", continuation_policy="async", now=NOW + 2)
    assert repo.current_wake_policy("local/main", "async") is None


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
    aid = _real_attempt(repo)
    r = repo.accept_wake_dispatch(c["dispatch_id"], handoff_id=c["handoff_id"],
                                  attempt_id=aid, now=NOW + 1)
    assert r["accepted"] is True
    d = repo.get_wake_dispatch(c["dispatch_id"])
    assert d["status"] == "accepted" and d["accepted_at"] == NOW + 1
    assert d["attempt_id"] == aid
    row = repo.get_wake_intent("intent-1")
    assert row["status"] == "handed_off"
    assert row["handoff_id"] == c["handoff_id"]
    assert row["active_attempt_id"] == aid


def test_accept_attempt_id_required(repo):
    """seq2463③：attempt_id 空 / 不存在 → 拒绝（受控 ref，非任意字符串）。"""
    _intent(repo)
    c = _claim(repo)
    assert repo.accept_wake_dispatch(c["dispatch_id"], handoff_id=c["handoff_id"],
                                     attempt_id="", now=NOW + 1)["reason"] == "attempt_id_required"
    assert repo.accept_wake_dispatch(c["dispatch_id"], handoff_id=c["handoff_id"],
                                     attempt_id="not-a-real-attempt",
                                     now=NOW + 1)["reason"] == "attempt_not_found"


def test_accept_idempotent_on_duplicate_delivery(repo):
    """重复送达（同 handoff + 同 attempt）→ 幂等成功。"""
    _intent(repo)
    c = _claim(repo)
    aid = _real_attempt(repo)
    r1 = repo.accept_wake_dispatch(c["dispatch_id"], handoff_id=c["handoff_id"],
                                   attempt_id=aid, now=NOW + 1)
    r2 = repo.accept_wake_dispatch(c["dispatch_id"], handoff_id=c["handoff_id"],
                                   attempt_id=aid, now=NOW + 2)
    assert r1["accepted"] is True and r2["accepted"] is True and r2["idempotent"] is True
    # 不同 attempt 的重复投递 → 拒绝（防旧 attempt 抢占）
    aid2 = _real_attempt(repo)
    r3 = repo.accept_wake_dispatch(c["dispatch_id"], handoff_id=c["handoff_id"],
                                   attempt_id=aid2, now=NOW + 3)
    assert r3["accepted"] is False


def test_accept_attempt_owner_mismatch_rejected(repo):
    """seq2466②：attempt 归属校验——跨 owner 的 attempt 不能绑到本 dispatch。"""
    _intent(repo, "ow-1", "k-ow", owner_id="other-user")  # intent 属 other-user
    c = _claim(repo, "ow-1")
    aid = _real_attempt(repo)  # attempt 属 local/main
    r = repo.accept_wake_dispatch(c["dispatch_id"], handoff_id=c["handoff_id"],
                                  attempt_id=aid, now=NOW + 1)
    assert r["accepted"] is False and r["reason"] == "attempt_owner_mismatch"
    assert repo.get_wake_intent("ow-1")["status"] == "claimed"


def test_accept_attempt_task_mismatch_rejected(repo):
    """seq2466②：intent 绑定 task 时，attempt 属其他 task → 拒绝。"""
    task_a = repo.create_task(owner_id="local/main", title="a")
    task_b = repo.create_task(owner_id="local/main", title="b")
    tr_a = repo.create_task_run(task_id=task_a["task_id"])
    tr_b = repo.create_task_run(task_id=task_b["task_id"])
    ar_b = repo.create_agent_run(task_run_id=tr_b["task_run_id"], role="main")
    attempt_b = repo.create_attempt(ar_b["agent_run_id"])
    _intent(repo, "tm-1", "k-tm", task_id=task_a["task_id"])  # intent 绑 task_a
    c = _claim(repo, "tm-1")
    r = repo.accept_wake_dispatch(c["dispatch_id"], handoff_id=c["handoff_id"],
                                  attempt_id=str(attempt_b["attempt_id"]), now=NOW + 1)
    assert r["accepted"] is False and r["reason"] == "attempt_task_mismatch"
    assert repo.get_wake_intent("tm-1")["status"] == "claimed"


def test_accept_wrong_handoff_rejected(repo):
    _intent(repo)
    c = _claim(repo)
    aid = _real_attempt(repo)
    r = repo.accept_wake_dispatch(c["dispatch_id"], handoff_id="wrong-handoff",
                                  attempt_id=aid, now=NOW + 1)
    assert r["accepted"] is False and r["reason"] == "handoff_mismatch"
    assert repo.get_wake_intent("intent-1")["status"] == "claimed"


def test_accept_lease_expired_late_receipt_to_reconciliation(repo):
    """lease 已过期的迟到 receipt → 转 reconciliation，不把旧 intent 标 handed_off。"""
    _intent(repo)
    c = _claim(repo)
    aid = _real_attempt(repo)
    r = repo.accept_wake_dispatch(c["dispatch_id"], handoff_id=c["handoff_id"],
                                  attempt_id=aid, now=NOW + 301)  # lease_until=NOW+300 已过期
    assert r["accepted"] is False
    assert r["reason"] == "lease_expired_reconciliation"
    d = repo.get_wake_dispatch(c["dispatch_id"])
    assert d["status"] == "reconciliation"
    row = repo.get_wake_intent("intent-1")
    assert row["status"] == "claimed"  # 未被标 handed_off


def test_accept_intent_generation_mismatch_rejected(repo):
    _intent(repo)
    c = _claim(repo)
    # 模拟 intent 已 release 回 pending 且换代（generation 变化）→ 迟到 accept 拒绝
    repo.release_wake_dispatch(c["dispatch_id"], claim_token=c["claim_token"],
                               expected_generation=c["claim_generation"],
                               lease_owner="gw-1", now=NOW + 301)
    repo.release_wake_intent_lease("intent-1", claim_token=c["claim_token"],
                                   expected_generation=c["claim_generation"], now=NOW + 301)
    r = repo.accept_wake_dispatch(c["dispatch_id"], handoff_id=c["handoff_id"],
                                  attempt_id=_real_attempt(repo), now=NOW + 302)
    assert r["accepted"] is False


def test_claim_blocked_when_active_attempt(repo):
    """同 intent 已有 active attempt → 新 claim 拒绝（active_attempt_id 门）。"""
    _intent(repo)
    c = _claim(repo)
    aid = _real_attempt(repo)
    repo.accept_wake_dispatch(c["dispatch_id"], handoff_id=c["handoff_id"],
                              attempt_id=aid, now=NOW + 1)
    c2 = _claim(repo, owner="gw-2", now=NOW + 2)
    assert c2["claimed"] is False


def test_release_pair_returns_to_pending(repo):
    """lease 过期 + 无已知副作用 → release dispatch + intent，代际递增防旧认领。"""
    _intent(repo)
    c = _claim(repo)
    r = repo.release_wake_dispatch(c["dispatch_id"], claim_token=c["claim_token"],
                                   expected_generation=c["claim_generation"],
                                   lease_owner="gw-1", now=NOW + 301)
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


def test_release_requires_owned_dispatch(repo):
    """seq2463⑤：终结 CAS 校验 claim_token/generation/lease_owner——
    错误身份（错误 token/lease_owner）不能终结别人的 dispatch。"""
    _intent(repo)
    c = _claim(repo)
    # 错误 claim_token
    assert repo.release_wake_dispatch(
        c["dispatch_id"], claim_token="wrong-token",
        expected_generation=c["claim_generation"], lease_owner="gw-1",
        now=NOW + 301)["released"] is False
    # 错误 lease_owner
    assert repo.release_wake_dispatch(
        c["dispatch_id"], claim_token=c["claim_token"],
        expected_generation=c["claim_generation"], lease_owner="gw-2",
        now=NOW + 301)["released"] is False
    # 正确身份 → 成功
    assert repo.release_wake_dispatch(
        c["dispatch_id"], claim_token=c["claim_token"],
        expected_generation=c["claim_generation"], lease_owner="gw-1",
        now=NOW + 301)["released"] is True


def test_release_blocked_by_pending_dispatch(repo):
    """seq2461 锁定①：claim 后到 acceptance 前 lease 回收不得绕过 active gate——
    存在未完成的 dispatched dispatch 记录时 release 拒绝（等价阻止重认领）。"""
    _intent(repo)
    c = _claim(repo)
    ri = repo.release_wake_intent_lease("intent-1", claim_token=c["claim_token"],
                                        expected_generation=c["claim_generation"],
                                        now=NOW + 301)
    assert ri["released"] is False
    assert repo.get_wake_intent("intent-1")["status"] == "claimed"
    # dispatch 终态化（fail）后 → release 放行回 pending（可被 reconciler 重认领）
    repo.fail_wake_dispatch(c["dispatch_id"], error_ref="reconciled",
                            claim_token=c["claim_token"],
                            expected_generation=c["claim_generation"],
                            lease_owner="gw-1", now=NOW + 302)
    ri2 = repo.release_wake_intent_lease("intent-1", claim_token=c["claim_token"],
                                         expected_generation=c["claim_generation"],
                                         now=NOW + 303)
    assert ri2["released"] is True
    assert repo.get_wake_intent("intent-1")["status"] == "pending"


def test_release_requires_no_active_attempt(repo):
    """active attempt 未清 → release 拒绝（防未知副作用被误释放）。"""
    _intent(repo)
    c = _claim(repo)
    aid = _real_attempt(repo)
    repo.accept_wake_dispatch(c["dispatch_id"], handoff_id=c["handoff_id"],
                              attempt_id=aid, now=NOW + 1)
    ri = repo.release_wake_intent_lease("intent-1", claim_token=c["claim_token"],
                                        expected_generation=c["claim_generation"],
                                        now=NOW + 301)
    assert ri["released"] is False


def test_clear_active_attempt_cas(repo):
    _intent(repo)
    c = _claim(repo)
    aid = _real_attempt(repo)
    repo.accept_wake_dispatch(c["dispatch_id"], handoff_id=c["handoff_id"],
                              attempt_id=aid, now=NOW + 1)
    assert repo.clear_wake_intent_active_attempt("intent-1", attempt_id="wrong",
                                                 now=NOW + 2)["cleared"] is False
    assert repo.clear_wake_intent_active_attempt("intent-1", attempt_id=aid,
                                                 now=NOW + 2)["cleared"] is True
    assert repo.get_wake_intent("intent-1")["active_attempt_id"] == ""


def test_crash_after_claim_intent_stays_claimed(repo):
    """crash-after-claim：claim+outbox 已持久但未 accept → intent 保持 claimed +
    dispatch 保持 dispatched，reconciler 可见（不丢、不误标 handed_off）。"""
    _intent(repo)
    c = _claim(repo)
    row = repo.get_wake_intent("intent-1")
    assert row["status"] == "claimed"
    assert row["active_attempt_id"] == ""
    d = repo.get_wake_dispatch(c["dispatch_id"])
    assert d["status"] == "dispatched"
    r = repo.mark_wake_dispatch_reconciliation(
        c["dispatch_id"], error_ref="crash_recovered", claim_token=c["claim_token"],
        expected_generation=c["claim_generation"], lease_owner="gw-1", now=NOW + 301)
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
    r = repo.fail_wake_dispatch(c["dispatch_id"], error_ref="pool_full",
                                claim_token=c["claim_token"],
                                expected_generation=c["claim_generation"],
                                lease_owner="gw-1", now=NOW + 1)
    assert r["failed"] is True
    assert repo.get_wake_dispatch(c["dispatch_id"])["status"] == "failed"
    assert repo.get_wake_intent("intent-1")["status"] == "claimed"


# -------------------------------------------------- 存量库迁移（seq2466①）
OLD_WAKE_POLICIES = """
CREATE TABLE wake_policies (
    policy_id TEXT PRIMARY KEY,
    owner_id TEXT NOT NULL,
    scope_ref TEXT NOT NULL DEFAULT '',
    continuation_policy TEXT NOT NULL,
    policy_generation INTEGER NOT NULL DEFAULT 1,
    allowed_sources TEXT NOT NULL DEFAULT '',
    provider_scope_ref TEXT NOT NULL DEFAULT '',
    revoked_at REAL NOT NULL DEFAULT 0,
    updated_at REAL NOT NULL,
    UNIQUE(owner_id, continuation_policy)
)"""


def test_legacy_wake_policies_constraint_rebuilt(tmp_path):
    """seq2466①：4945883f 旧 schema（表级 UNIQUE）启动 → init 表重建移除旧约束，
    历史保留修复在真实存量库可用（旧行 revoked + 新 generation INSERT 不撞约束）。"""
    import sqlite3
    from pathlib import Path
    db = tmp_path / "home" / "runtime.db"
    db.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db)
    conn.executescript(OLD_WAKE_POLICIES)
    conn.execute(
        "INSERT INTO wake_policies (policy_id, owner_id, scope_ref, continuation_policy,"
        " policy_generation, allowed_sources, provider_scope_ref, revoked_at, updated_at)"
        " VALUES ('p1', 'local/main', 'local/main', 'async', 1, 'cron', 'opencode', 0, ?)",
        (NOW,),
    )
    conn.commit()
    conn.close()
    repo = RuntimeRepository(db)  # init：_execute_runtime_schema + _apply_runtime_migrations
    # 表级 UNIQUE 已移除（sqlite_master 无 UNIQUE(owner_id, continuation_policy)）
    conn = sqlite3.connect(db)
    create_sql = str(conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='wake_policies'"
    ).fetchone()[0])
    conn.close()
    assert "UNIQUE(owner_id, continuation_policy)" not in create_sql
    # partial unique index 存在
    with repo._runtime_connection() as rconn:
        idx = rconn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND name='uq_wake_policies_active'"
        ).fetchone()
        assert idx is not None
    # 旧行 revoked + 新 generation INSERT 不撞旧约束 → 历史保留修复可用
    repo.set_wake_policy(policy_id="p2", owner_id="local/main",
                         continuation_policy="async", policy_generation=2,
                         allowed_sources="cron", provider_scope_ref="opencode",
                         scope_ref="local/main", now=NOW + 1)
    cur = repo.current_wake_policy("local/main", "async")
    assert cur["policy_id"] == "p2" and cur["policy_generation"] == 2
    with repo._runtime_connection() as rconn:
        rows = rconn.execute(
            "SELECT policy_id, revoked_at FROM wake_policies WHERE continuation_policy='async'"
        ).fetchall()
    ids = {r["policy_id"]: r["revoked_at"] for r in rows}
    assert "p1" in ids and int(ids["p1"]) > 0  # 历史行保留 + 已 revoked
