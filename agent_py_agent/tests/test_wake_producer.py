"""wake_intent producer 统一入口测试（#233 第 3/4 步）。

覆盖：
- dedup_key 规格 §3 稳定计算（retry 优先；不含 attempt_id）。
- register_wake_intent fail-closed（bad source / interactive 禁周期来源 /
  空 provider_scope_ref / 负 generation / 空 task_id → 结构化拒绝不落库）。
- ensure_wake_policy 注册 + fail-closed（空白名单/空 scope 拒绝）。
- 端到端：policy 注册 → intent 写入 → dispatcher claim → 真实 attempt
  accept → handed_off（producer→dispatcher→acceptance 全链路）。
"""

from __future__ import annotations

import pytest

from agent_py_agent.agent.runtime_db.repository import RuntimeRepository
from agent_py_agent.agent.wake_dispatcher import (
    dispatch_due_wake_intent,
)
from agent_py_agent.agent.wake_producer import (
    compute_wake_intent_dedup_key,
    ensure_wake_policy,
    register_wake_intent,
)

NOW = 1_700_000_000.0


@pytest.fixture
def repo(tmp_path):
    return RuntimeRepository(tmp_path / "home" / "runtime.db")


def _real_task(repo):
    task = repo.create_task(owner_id="local/main", title="producer-test")
    return task["task_id"]


def _real_attempt_for_task(repo, task_id):
    task_run = repo.create_task_run(task_id=task_id)
    agent_run = repo.create_agent_run(task_run_id=task_run["task_run_id"], role="main")
    attempt = repo.create_attempt(agent_run["agent_run_id"])
    return str(attempt["attempt_id"])


# ------------------------------------------------------- dedup_key
def test_dedup_key_stable_and_retry_priority(repo):
    k1 = compute_wake_intent_dedup_key(
        owner_id="u1", task_id="task-x", run_id="", policy_generation=2,
        source="cron", wake_reason="cron_due", source_event_id="ev-1",
        due_window="w1",
    )
    k2 = compute_wake_intent_dedup_key(
        owner_id="u1", task_id="task-x", run_id="", policy_generation=2,
        source="cron", wake_reason="cron_due", source_event_id="ev-1",
        due_window="w1",
    )
    assert k1 == k2  # 稳定
    k_retry = compute_wake_intent_dedup_key(
        owner_id="u1", task_id="task-x", run_id="", policy_generation=2,
        source="cron", wake_reason="cron_due", source_event_id="ev-1",
        retry_event_id="ev-retry", due_window="w1",
    )
    assert k_retry != k1  # retry_event_id 优先 → 新 key


# ------------------------------------------------------- register_wake_intent
def test_register_async_cron_ok_and_dedup_idempotent(repo):
    task_id = _real_task(repo)
    r1 = register_wake_intent(
        repo, owner_id="local/main", task_id=task_id, source="cron",
        wake_reason="cron_due", continuation_policy="async",
        provider_scope_ref="opencode", policy_generation=1,
        next_wake_at=NOW - 1, source_event_id="ev-cron-1", due_window="w1",
        now=NOW,
    )
    assert r1["created"] is True and r1["intent_id"]
    row = repo.get_wake_intent(r1["intent_id"])
    assert row["source"] == "cron" and row["continuation_policy"] == "async"
    assert row["status"] == "pending"
    # 同 source_event_id 重复投递 → 幂等返回已存在
    r2 = register_wake_intent(
        repo, owner_id="local/main", task_id=task_id, source="cron",
        wake_reason="cron_due", continuation_policy="async",
        provider_scope_ref="opencode", policy_generation=1,
        next_wake_at=NOW - 1, source_event_id="ev-cron-1", due_window="w1",
        now=NOW,
    )
    assert r2["created"] is False and r2["existing"] is True
    assert r2["intent_id"] == r1["intent_id"]


def test_register_rejects_bad_source(repo):
    task_id = _real_task(repo)
    with pytest.raises(ValueError):
        register_wake_intent(
            repo, owner_id="local/main", task_id=task_id, source="mystery",
            wake_reason="x", continuation_policy="async",
            provider_scope_ref="opencode", policy_generation=1,
            next_wake_at=NOW, now=NOW,
        )


def test_register_rejects_interactive_periodic_source(repo):
    """interactive 只允许事件类来源（seq2416）：interactive+cron → 拒绝。"""
    task_id = _real_task(repo)
    with pytest.raises(ValueError):
        register_wake_intent(
            repo, owner_id="local/main", task_id=task_id, source="cron",
            wake_reason="cron_due", continuation_policy="interactive",
            provider_scope_ref="opencode", policy_generation=1,
            next_wake_at=NOW, now=NOW,
        )


def test_register_rejects_missing_scope_and_bad_generation(repo):
    task_id = _real_task(repo)
    with pytest.raises(ValueError):
        register_wake_intent(
            repo, owner_id="local/main", task_id=task_id, source="cron",
            wake_reason="cron_due", continuation_policy="async",
            provider_scope_ref="", policy_generation=1, next_wake_at=NOW,
            now=NOW,
        )
    with pytest.raises(ValueError):
        register_wake_intent(
            repo, owner_id="local/main", task_id=task_id, source="cron",
            wake_reason="cron_due", continuation_policy="async",
            provider_scope_ref="opencode", policy_generation=-1,
            next_wake_at=NOW, now=NOW,
        )


def test_register_rejects_missing_task_id(repo):
    with pytest.raises(ValueError):
        register_wake_intent(
            repo, owner_id="local/main", task_id="", source="cron",
            wake_reason="cron_due", continuation_policy="async",
            provider_scope_ref="opencode", policy_generation=1,
            next_wake_at=NOW, now=NOW,
        )


def test_register_inbound_interactive_ok(repo):
    task_id = _real_task(repo)
    r = register_wake_intent(
        repo, owner_id="local/main", task_id=task_id, source="inbound",
        wake_reason="user_message", continuation_policy="interactive",
        provider_scope_ref="opencode", policy_generation=1,
        next_wake_at=NOW, source_event_id="ev-inbound-1", due_window="w1",
        now=NOW,
    )
    assert r["created"] is True


# ------------------------------------------------------- ensure_wake_policy
def test_ensure_wake_policy_ok_and_fail_closed(repo):
    r = ensure_wake_policy(
        repo, owner_id="local/main", continuation_policy="async",
        policy_generation=1, allowed_sources="cron,heartbeat",
        provider_scope_ref="opencode", scope_ref="local/main", now=NOW,
    )
    assert r["set"] is True
    # 空白名单 / 空 scope → 结构化拒绝
    with pytest.raises(ValueError):
        ensure_wake_policy(
            repo, owner_id="local/main", continuation_policy="async",
            policy_generation=1, allowed_sources="",
            provider_scope_ref="opencode", scope_ref="local/main", now=NOW,
        )
    with pytest.raises(ValueError):
        ensure_wake_policy(
            repo, owner_id="local/main", continuation_policy="async",
            policy_generation=1, allowed_sources="cron",
            provider_scope_ref="opencode", scope_ref="", now=NOW,
        )


# ------------------------------------------------------- 端到端闭环
def test_producer_to_dispatcher_to_accept_end_to_end(repo):
    """policy 注册 → intent 写入 → dispatcher claim → 真实 attempt accept →
    handed_off（#233-3/4 最小闭环：producer 只写 intent，accept 才 handed_off）。"""
    task_id = _real_task(repo)
    # 1) 注册 canonical policy（dispatcher claim 的授权前提）
    ensure_wake_policy(
        repo, owner_id="local/main", continuation_policy="async",
        policy_generation=1, allowed_sources="cron",
        provider_scope_ref="opencode", scope_ref="local/main", now=NOW,
    )
    # 2) producer 写 intent（next_wake_at 过去 → due）
    r = register_wake_intent(
        repo, owner_id="local/main", task_id=task_id, source="cron",
        wake_reason="cron_due", continuation_policy="async",
        provider_scope_ref="opencode", policy_generation=1,
        next_wake_at=NOW - 1, source_event_id="ev-e2e-1", due_window="w1",
        now=NOW,
    )
    intent_id = r["intent_id"]
    row = repo.get_wake_intent(intent_id)
    assert row["status"] == "pending"
    # 3) dispatcher claim（真实 policy/circuit 注入）
    out = dispatch_due_wake_intent(
        repo, row, lease_owner="gw-e2e", lease_seconds=300, now=NOW,
        policy_validator=lambda rw: repo.wake_policy_allows(rw),
        provider_circuit_open=lambda scope: repo.provider_circuit_frozen(scope, now=NOW),
    )
    assert out.claimed, out.reason
    assert out.dispatch_id
    assert repo.get_wake_intent(intent_id)["status"] == "claimed"
    # 4) 执行席创建真实 attempt → accept → handed_off
    aid = _real_attempt_for_task(repo, task_id)
    acc = repo.accept_wake_dispatch(out.dispatch_id, handoff_id=out.handoff_id,
                                    attempt_id=aid, now=NOW + 1)
    assert acc["accepted"] is True, acc
    final = repo.get_wake_intent(intent_id)
    assert final["status"] == "handed_off"
    assert final["active_attempt_id"] == aid
    d = repo.get_wake_dispatch(out.dispatch_id)
    assert d["status"] == "accepted" and d["attempt_id"] == aid
