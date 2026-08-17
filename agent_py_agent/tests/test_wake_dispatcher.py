"""wake_intent dispatcher 授权门禁测试（#233 第 2 步 / #236）。

覆盖：授权校验矩阵（policy 缺失/来源越权/provider_scope_ref 缺失/空值
fail-closed）+ claim 成功路径 + 被拒 intent 保持 pending 可审计。
"""

from __future__ import annotations

import pytest

from agent_py_agent.agent.runtime_db.repository import RuntimeRepository
from agent_py_agent.agent.wake_dispatcher import (
    dispatch_due_wake_intent,
    validate_wake_intent_authorization,
)

NOW = 1_700_000_000.0


@pytest.fixture
def repo(tmp_path):
    return RuntimeRepository(tmp_path / "home" / "runtime.db")


def _intent(repo, intent_id, dedup_key, source="cron", wake_reason="cron_due",
            policy="async", generation=1, scope="opencode", **kw):
    kw.setdefault("next_wake_at", NOW - 1)
    return repo.create_wake_intent(
        intent_id=intent_id, dedup_key=dedup_key, owner_id="local/main",
        source=source, wake_reason=wake_reason,
        continuation_policy=policy, policy_generation=generation,
        provider_scope_ref=scope, now=NOW, **kw,
    )


def _ok_validator(row):
    return True


def _ok_circuit(scope):
    return False  # circuit 未冻结


def _dispatch(repo, intent_id, **kw):
    kw.setdefault("policy_validator", _ok_validator)
    kw.setdefault("provider_circuit_open", _ok_circuit)
    return dispatch_due_wake_intent(repo, repo.get_wake_intent(intent_id),
                                    lease_owner="gw-1", lease_seconds=300, now=NOW, **kw)


def test_authorization_async_cron_ok(repo):
    _intent(repo, "a1", "k1")
    ok, reason = validate_wake_intent_authorization(
        repo.get_wake_intent("a1"),
        policy_validator=_ok_validator,
        provider_circuit_open=_ok_circuit,
    )
    assert ok, reason
    out = _dispatch(repo, "a1")
    assert out.claimed and out.reason == "dispatched"
    assert repo.get_wake_intent("a1")["status"] == "claimed"


def test_authorization_policy_missing_rejected(repo):
    _intent(repo, "b1", "k2", policy="")
    out = _dispatch(repo, "b1")
    assert not out.claimed
    assert "rejected_policy_missing_or_invalid" in out.reason
    # 被拒 intent 保持 pending + 可审计（reconciler 可见）
    row = repo.get_wake_intent("b1")
    assert row["status"] == "pending"
    assert "authorization:policy_missing_or_invalid" in row["last_error_ref"]


def test_authorization_interactive_cron_rejected(repo):
    """interactive 禁周期来源（cron 属 async）——seq2416/2420 修正。"""
    _intent(repo, "c1", "k3", source="cron", policy="interactive")
    out = _dispatch(repo, "c1")
    assert not out.claimed
    assert "rejected_source_not_allowed_for_interactive" in out.reason


def test_authorization_async_missing_scope_rejected(repo):
    _intent(repo, "d1", "k4", scope="")
    out = _dispatch(repo, "d1")
    assert not out.claimed
    assert "rejected_provider_scope_ref_missing" in out.reason


def test_authorization_invalid_generation_rejected(repo):
    _intent(repo, "e1", "k5", generation=-1)
    out = _dispatch(repo, "e1")
    assert not out.claimed
    assert "rejected_policy_generation_missing" in out.reason


def test_authorization_unknown_source_rejected(repo):
    _intent(repo, "f1", "k6", source="mystery")
    out = _dispatch(repo, "f1")
    assert not out.claimed
    assert "rejected_source_not_authorized" in out.reason


def test_authorization_interactive_inbound_ok(repo):
    """interactive inbound 是唯一允许的交互来源（C：入站走 dispatcher 管线）。
    规格 §1：provider_scope_ref 是所有 intent 必填（provider circuit 统一路由），
    interactive 也强制非空（seq2436）。"""
    _intent(repo, "g1", "k7", source="inbound", wake_reason="user_message",
            policy="interactive", scope="provider-opencode")
    out = _dispatch(repo, "g1")
    assert out.claimed and out.reason == "dispatched"


def test_claim_fails_when_not_due(repo):
    _intent(repo, "h1", "k8", next_wake_at=NOW + 100)
    out = _dispatch(repo, "h1")
    assert not out.claimed
    assert "not_pending_or_not_due" in out.reason


def test_no_injected_gate_rejected_fail_closed(repo):
    """无注入必拒（seq2450 大橘硬门）：policy_validator / provider_circuit_open
    未注入 → 结构化拒绝，保持 pending 可审计，绝不兜底放行。"""
    _intent(repo, "i1", "k9")
    out = _dispatch(repo, "i1", policy_validator=None, provider_circuit_open=None)
    assert not out.claimed
    assert out.reason == "rejected_policy_validator_required"
    row = repo.get_wake_intent("i1")
    assert row["status"] == "pending"
    assert "authorization:policy_validator_required" in row["last_error_ref"]
    # 有 policy 无 circuit → 同样拒绝（两者都必传）
    _intent(repo, "i2", "k9b")
    out = _dispatch(repo, "i2", policy_validator=_ok_validator, provider_circuit_open=None)
    assert not out.claimed
    assert out.reason == "rejected_provider_circuit_required"
    assert repo.get_wake_intent("i2")["status"] == "pending"


def test_no_injected_gate_still_rejects_bad_fields(repo):
    """无注入也不放行坏字段：字段级门始终 fail-closed。"""
    _intent(repo, "j1", "k10", policy="")  # policy 空 → 字段门拒绝
    out = _dispatch(repo, "j1", policy_validator=None, provider_circuit_open=None)
    assert not out.claimed
    assert "rejected_policy_missing_or_invalid" in out.reason
    _intent(repo, "j2", "k11", scope="", generation=-1)  # scope 空 + generation -1
    out = _dispatch(repo, "j2", policy_validator=None, provider_circuit_open=None)
    assert not out.claimed


def test_injected_validator_rejection_still_fail_closed(repo):
    """有注入时注入核对仍 fail-closed（不因弹性门放松）——policy ledger 撤销/异常拒绝。"""
    _intent(repo, "l1", "k12", generation=3)
    out = _dispatch(repo, "l1", policy_validator=lambda row: False)
    assert not out.claimed
    assert "rejected_policy_generation_not_current_or_revoked" in out.reason
    _intent(repo, "l2", "k13")
    out = _dispatch(repo, "l2", policy_validator=lambda row: (_ for _ in ()).throw(RuntimeError("boom")))
    assert not out.claimed
    assert "rejected_policy_validator_error" in out.reason
    # circuit 冻结 → 拒绝
    _intent(repo, "l3", "k14")
    out = _dispatch(repo, "l3", provider_circuit_open=lambda s: True)
    assert not out.claimed
    assert "rejected_provider_circuit_open_or_quota_frozen" in out.reason


def test_claim_records_dispatch_ledger(repo):
    """claim 成功必须同事务落 wake_dispatches outbox（唯一 event/handoff id）。"""
    _intent(repo, "m1", "k15")
    out = _dispatch(repo, "m1")
    assert out.claimed
    assert out.dispatch_id and out.dispatch_event_id and out.handoff_id
    row = repo.get_wake_intent("m1")
    assert row["status"] == "claimed"  # claim 不等于 handoff
    dispatches = repo.wake_dispatches_for_intent("m1")
    assert len(dispatches) == 1
    d = dispatches[0]
    assert d["status"] == "dispatched"
    assert d["dispatch_event_id"] == out.dispatch_event_id
    assert d["handoff_id"] == out.handoff_id
    assert d["claim_generation"] == out.generation
