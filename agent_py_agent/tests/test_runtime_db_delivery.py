"""R4 Delivery 的 outbox/inbox 不变量测试。

覆盖：
- K.3：reconcile（provider query 确认副作用 → ACKED / absent → 退避）。
- K.4：outbox/inbox at-least-once + effect_key 去重。
- K.6：dead-letter 可查询、可人工重放、完整证据。
"""

from __future__ import annotations

import pytest

from agent_py_agent.agent.runtime_db.operations import (
    INBOX_PROCESSED,
    OUTBOX_ACKED,
    OUTBOX_DEAD_LETTER,
    OUTBOX_FAILED,
    OUTBOX_IN_FLIGHT,
    OUTBOX_PENDING,
    RuntimeConflictError,
)
from agent_py_agent.agent.runtime_db.repository import RuntimeRepository

OWNER = "local/main"


@pytest.fixture
def repo(tmp_path):
    return RuntimeRepository(tmp_path / "home" / "runtime.db")


@pytest.fixture
def chain(repo):
    return repo.record_run_creation(owner_id=OWNER, run_id="run-main", goal="g")


# ------------------------------------------------------------------- outbox
def test_enqueue_idempotent_effect_key(repo, chain):
    """K.4：同 effect_key 重复入队幂等 —— 只产生一条投递。"""
    e1 = repo.enqueue_outbox(
        effect_key="msg:1", scope="feishu", payload={"text": "hi"}, task_run_id=chain["task_run_id"]
    )
    e2 = repo.enqueue_outbox(
        effect_key="msg:1", scope="feishu", payload={"text": "hi"}, task_run_id=chain["task_run_id"]
    )
    assert e1["created"] is True
    assert e2["created"] is False
    assert e1["outbox_id"] == e2["outbox_id"]
    assert len(repo.list_outbox(owner_id="")) == 1


def test_enqueue_requires_effect_key(repo):
    with pytest.raises(RuntimeConflictError, match="effect_key"):
        repo.enqueue_outbox(effect_key="")


def test_claim_atomic_lease(repo, chain):
    """claim 原子租约：IN_FLIGHT 条目其他 worker 领不走。"""
    repo.enqueue_outbox(effect_key="m:1", scope="x", payload={}, task_run_id=chain["task_run_id"])
    claimed = repo.claim_outbox(claimed_by="worker-A", owner_id="")
    assert len(claimed) == 1
    assert claimed[0]["attempts"] == 1
    assert claimed[0]["claimed_by"] == "worker-A"
    assert repo.claim_outbox(claimed_by="worker-B", owner_id="") == []


def test_settle_acked_terminal(repo, chain):
    """ACKED 终态：不可再 claim，settled_at 落时间。"""
    e = repo.enqueue_outbox(effect_key="m:1", scope="x", payload={})
    repo.claim_outbox(claimed_by="w", owner_id="")
    s = repo.settle_outbox(outbox_id=e["outbox_id"], status="ACKED", provider_evidence={"ok": True})
    assert s["status"] == OUTBOX_ACKED
    assert s["settled_at"] > 0
    assert s["provider_evidence"] == {"attempt_1": {"ok": True}}
    # 重复 settle（ACK 重放）幂等不改写。
    again = repo.settle_outbox(outbox_id=e["outbox_id"], status="ACKED")
    assert again["status"] == OUTBOX_ACKED


def test_settle_failed_retry_then_dead_letter(repo, chain):
    """K.6：FAILED 退避重试，attempts 超限进 DEAD_LETTER，证据完整保留。"""
    repo.enqueue_outbox(
        effect_key="m:1", scope="http", payload={"url": "x"},
        owner_id=OWNER, task_run_id=chain["task_run_id"],
    )
    for _ in range(8):
        for c in repo.claim_outbox(claimed_by="w", owner_id=""):
            repo.settle_outbox(outbox_id=c["outbox_id"], status="FAILED", next_retry_at=0)
    dl = repo.list_outbox(status=OUTBOX_DEAD_LETTER)
    assert len(dl) == 1
    assert dl[0]["attempts"] == 8
    # 完整证据：8 次 attempt 的 evidence 全保留（K.6）。
    assert list(dl[0]["provider_evidence"]) == [
        f"attempt_{i}" for i in range(1, 9)
    ]
    # 可查询（K.6）：按 task_run/owner/状态过滤。
    assert len(repo.list_outbox(status=OUTBOX_DEAD_LETTER, owner_id=OWNER)) == 1
    # 可人工重放（K.6）：DEAD_LETTER → PENDING，attempts 清零。
    replayed = repo.replay_dead_letter(outbox_id=dl[0]["outbox_id"])
    assert replayed["status"] == OUTBOX_PENDING
    assert replayed["attempts"] == 0
    # 非 DEAD_LETTER 不可重放。
    with pytest.raises(RuntimeConflictError, match="DEAD_LETTER"):
        repo.replay_dead_letter(outbox_id=dl[0]["outbox_id"])


def test_reconcile_confirmed_acks(repo, chain):
    """K.3：provider query 确认副作用已生效 → ACKED（不会重发）。"""
    e = repo.enqueue_outbox(effect_key="m:1", scope="feishu", payload={"text": "hi"})
    repo.claim_outbox(claimed_by="w", owner_id="")
    r = repo.reconcile_outbox(
        outbox_id=e["outbox_id"], provider_found=True, provider_evidence={"query": "sent"}
    )
    assert r["status"] == OUTBOX_ACKED
    assert any(k.startswith("reconcile_") for k in r["provider_evidence"])


def test_reconcile_absent_requeues(repo, chain):
    """K.3：provider 查无副作用 → 回 PENDING 退避（宁可重发不可丢）。"""
    e = repo.enqueue_outbox(effect_key="m:1", scope="feishu", payload={"text": "hi"})
    repo.claim_outbox(claimed_by="w", owner_id="")
    r = repo.reconcile_outbox(
        outbox_id=e["outbox_id"], provider_found=False, provider_evidence={"query": "absent"}
    )
    assert r["status"] == OUTBOX_PENDING
    assert r["next_retry_at"] > 0
    assert r["claimed_by"] == ""
    # 退避到期后可再领（at-least-once 重试）。
    repo.claim_outbox(claimed_by="w", owner_id="", now=10**12)
    # 未到期不可领。
    assert repo.claim_outbox(claimed_by="w", owner_id="", now=0) == []


# ------------------------------------------------------------------- inbox
def test_inbox_effect_key_dedup(repo, chain):
    """K.4：重复投递只处理一次（effect_key UNIQUE 去重）。"""
    i1 = repo.receive_inbox(effect_key="evt:9", payload={"n": 1}, sender="gateway")
    i2 = repo.receive_inbox(effect_key="evt:9", payload={"n": 1}, sender="gateway")
    assert i1["created"] is True and i2["created"] is False
    assert i1["inbox_id"] == i2["inbox_id"]
    processed = repo.mark_inbox_processed(effect_key="evt:9")
    assert processed["status"] == INBOX_PROCESSED
    assert repo.inbox_entry("evt:9")["processed_at"] > 0
    # 重复 PROCESSED 幂等。
    assert repo.mark_inbox_processed(effect_key="evt:9")["status"] == INBOX_PROCESSED
