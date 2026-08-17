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


def _ok_validator(policy, generation):
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
