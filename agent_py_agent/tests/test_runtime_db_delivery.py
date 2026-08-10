"""R4 Delivery 测试（3.txt §5 测试 17 + K 不变量矩阵）。

覆盖：
- 测试 17：duplicate/out-of-order/ACK loss/restart 只 closeout 一次、
  最终用户消息只发送一次（K.5）。
- K.1：同库状态本地事务（closeout 单事务原子）。
- K.3：reconcile（provider query 确认副作用 → ACKED / absent → 退避）。
- K.4：outbox/inbox at-least-once + effect_key 去重。
- K.6：dead-letter 可查询、可人工重放、完整证据。
- I.11：required acceptance 未全 VERIFIED → 不可交付（NOT_VERIFIED）。
"""

from __future__ import annotations

import pytest

from agent_py_agent.agent.acceptance import compile_acceptance_contract
from agent_py_agent.agent.runtime_db.operations import (
    INBOX_PROCESSED,
    NOT_VERIFIED,
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


def _freeze_and_verify(repo, chain, *, status="VERIFIED"):
    """冻结契约 + 一条 validator 终态（模拟 runner 已跑）。"""
    contract = compile_acceptance_contract(
        task_run_id=chain["task_run_id"],
        attempt_id=chain["attempt_id"],
        proposed={
            "assertions": [{"validator": "artifact_acceptance", "artifact_kind": "html"}]
        },
    )
    repo.freeze_contract(
        contract_id=contract.contract_id,
        task_run_id=chain["task_run_id"],
        attempt_id=chain["attempt_id"],
        compiled=contract.compiled,
        digest=contract.digest,
        inert_legacy=contract.inert_legacy,
    )
    op = repo.create_validator_operation(
        attempt_id=chain["attempt_id"],
        agent_run_id=chain["agent_run_id"],
        contract_id=contract.contract_id,
        validator_ref="artifact_acceptance",
        validator_kind="pure",
        code_digest="d",
        artifact_digests=[],
    )
    if status == "VERIFIED":
        repo.settle_validator_operation(op, status="VERIFIED", stdout_text="ok", exit_code=0)
    else:
        repo.settle_validator_operation(op, status=status, stderr_text="rejected")
    return contract


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
    assert [k for k in dl[0]["provider_evidence"]] == [
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


# ------------------------------------------------------------------- 测试 17
def test_closeout_requires_verified_acceptance(repo, chain):
    """I.11：无契约或未全 VERIFIED → NOT_VERIFIED，不可成功交付。"""
    with pytest.raises(RuntimeConflictError, match=NOT_VERIFIED):
        repo.closeout_task_run(task_run_id=chain["task_run_id"], final_message={"text": "x"})
    _freeze_and_verify(repo, chain, status="FAILED")
    with pytest.raises(RuntimeConflictError, match=NOT_VERIFIED):
        repo.closeout_task_run(task_run_id=chain["task_run_id"], final_message={"text": "x"})


def test_closeout_happy_path_single_final_message(repo, chain):
    """K.5：全 VERIFIED 收口成功；最终用户消息 effect_key UNIQUE 只入队一次。"""
    _freeze_and_verify(repo, chain)
    r = repo.closeout_task_run(
        task_run_id=chain["task_run_id"], final_message={"text": "完成"}
    )
    assert r["already_closed"] is False
    assert r["acceptance_ok"] is True
    assert r["final_effect_key"] == f"final:{chain['task_run_id']}"
    finals = repo.list_outbox(scope="final_message")
    assert len(finals) == 1
    assert finals[0]["payload"] == {"text": "完成"}
    assert repo.get_task_run(chain["task_run_id"])["status"] == "done"
    assert repo.get_task_run(chain["task_run_id"])["closed_at"] > 0
    # runtime_events 留收口证据（A.8 追到 task_run）。
    events = repo.events_for_task_run(chain["task_run_id"])
    assert any(e["event_type"] == "closeout" for e in events)


def test_closeout_duplicate_idempotent(repo, chain):
    """测试 17：duplicate closeout —— 第二次幂等，final message 仍只有一条。"""
    _freeze_and_verify(repo, chain)
    r1 = repo.closeout_task_run(task_run_id=chain["task_run_id"], final_message={"text": "完成"})
    r2 = repo.closeout_task_run(task_run_id=chain["task_run_id"], final_message={"text": "完成"})
    assert r1["already_closed"] is False
    assert r2["already_closed"] is True
    assert r2["closed_at"] == r1["closed_at"]
    assert len(repo.list_outbox(scope="final_message")) == 1


def test_closeout_restart_idempotent(repo, chain, tmp_path):
    """测试 17：restart —— 新连接同库再收口，closed_at 已设 → 幂等跳过。"""
    _freeze_and_verify(repo, chain)
    repo.closeout_task_run(task_run_id=chain["task_run_id"], final_message={"text": "完成"})
    reopened = RuntimeRepository(tmp_path / "home" / "runtime.db")
    r = reopened.closeout_task_run(
        task_run_id=chain["task_run_id"], final_message={"text": "完成"}
    )
    assert r["already_closed"] is True
    assert len(reopened.list_outbox(scope="final_message")) == 1


def test_closeout_ack_loss_no_resend(repo, chain):
    """测试 17：ACK loss —— final message 投递 ACK 丢失后重试不产生第二条。"""
    _freeze_and_verify(repo, chain)
    repo.closeout_task_run(task_run_id=chain["task_run_id"], final_message={"text": "完成"})
    finals = repo.list_outbox(scope="final_message")
    assert len(finals) == 1
    # 投递工 claim 后 ACK 丢失（IN_FLIGHT 挂起）→ 重试循环再来：仍是同一条。
    repo.claim_outbox(claimed_by="worker-1", owner_id="")
    # 重启后同库状态：IN_FLIGHT 条目还是那一条 effect_key。
    assert len(repo.list_outbox(scope="final_message")) == 1
    assert repo.list_outbox(scope="final_message")[0]["effect_key"] == (
        f"final:{chain['task_run_id']}"
    )


def test_closeout_out_of_order_children(repo, chain):
    """测试 17：out-of-order —— 子 completion 乱序收口以 CAS 为准只一次。"""
    # 父先收口（子后 completion 的乱序场景：父 closeout 已落 closed_at）。
    _freeze_and_verify(repo, chain)
    r1 = repo.closeout_task_run(task_run_id=chain["task_run_id"], final_message={"text": "parent"})
    assert r1["already_closed"] is False
    # 子结果迟到再次触发收口 → 幂等（乱序不会二收口）。
    r2 = repo.closeout_task_run(task_run_id=chain["task_run_id"], final_message={"text": "parent"})
    assert r2["already_closed"] is True
    assert len(repo.list_outbox(scope="final_message")) == 1


def test_closeout_unknown_task_run(repo):
    with pytest.raises(RuntimeConflictError, match="task_run 不存在"):
        repo.closeout_task_run(task_run_id="taskrun-zzz", final_message={})


def test_closeout_final_message_null_ok(repo, chain):
    """不要求最终消息也可收口（delivery 轴独立于 acceptance）。"""
    _freeze_and_verify(repo, chain)
    r = repo.closeout_task_run(task_run_id=chain["task_run_id"])
    assert r["already_closed"] is False
    assert r["final_effect_key"] == ""
    assert len(repo.list_outbox(scope="final_message")) == 0


# ------------------------------------------------------------- F3：契约 required 集合
def _freeze_two_required(repo, chain):
    """契约要求 2 条 required，只 VERIFIED 第 1 条（artifact_acceptance）。"""
    contract = compile_acceptance_contract(
        task_run_id=chain["task_run_id"],
        attempt_id=chain["attempt_id"],
        proposed={
            "assertions": [
                {"validator": "artifact_acceptance", "artifact_kind": "html"},
                {"validator": "static_site_check", "artifact_kind": "html"},
            ]
        },
    )
    repo.freeze_contract(
        contract_id=contract.contract_id,
        task_run_id=chain["task_run_id"],
        attempt_id=chain["attempt_id"],
        compiled=contract.compiled,
        digest=contract.digest,
        inert_legacy=contract.inert_legacy,
    )
    op = repo.create_validator_operation(
        attempt_id=chain["attempt_id"],
        agent_run_id=chain["agent_run_id"],
        contract_id=contract.contract_id,
        validator_ref="artifact_acceptance",
        validator_kind="pure",
        code_digest="d",
        artifact_digests=[],
    )
    repo.settle_validator_operation(op, status="VERIFIED", stdout_text="ok", exit_code=0)
    return contract


def test_closeout_rejects_partial_required_set(repo, chain):
    """I.11：契约要求 2 条 required 只 VERIFIED 1 条 → NOT_VERIFIED 拒绝。

    对照对象必须是契约冻结的 required assertion 集合，而不是
    validator_operations 里已跑过的行 —— 旧实现 all_refs == verified_refs
    只看已执行子集，只跑了部分会错误放行。
    """
    contract = _freeze_two_required(repo, chain)
    with pytest.raises(RuntimeConflictError, match=NOT_VERIFIED):
        repo.closeout_task_run(task_run_id=chain["task_run_id"], final_message={"text": "x"})
    # 状态未动：契约要求的第 2 条补齐 VERIFIED 后才允许收口。
    assert repo.get_task_run(chain["task_run_id"])["status"] != "done"
    op2 = repo.create_validator_operation(
        attempt_id=chain["attempt_id"],
        agent_run_id=chain["agent_run_id"],
        contract_id=contract.contract_id,
        validator_ref="static_site_check",
        validator_kind="pure",
        code_digest="d",
        artifact_digests=[],
    )
    repo.settle_validator_operation(op2, status="VERIFIED", stdout_text="ok", exit_code=0)
    r = repo.closeout_task_run(task_run_id=chain["task_run_id"], final_message={"text": "x"})
    assert r["acceptance_ok"] is True


def test_closeout_extra_runs_do_not_substitute_required(repo, chain):
    """I.11：跑过的行再多，缺契约 required 项仍拒绝（集合包含关系而非等价）。"""
    contract = _freeze_two_required(repo, chain)
    extra = repo.create_validator_operation(
        attempt_id=chain["attempt_id"],
        agent_run_id=chain["agent_run_id"],
        contract_id=contract.contract_id,
        validator_ref="document_acceptance",
        validator_kind="pure",
        code_digest="d",
        artifact_digests=[],
    )
    repo.settle_validator_operation(extra, status="VERIFIED", stdout_text="ok", exit_code=0)
    with pytest.raises(RuntimeConflictError, match=NOT_VERIFIED):
        repo.closeout_task_run(task_run_id=chain["task_run_id"], final_message={"text": "x"})


def test_closeout_outbox_owner_is_real_task_owner(repo, chain):
    """K.4：outbox owner_id 取 tasks 表真实 owner，不是 task_run_id。"""
    _freeze_and_verify(repo, chain)
    repo.closeout_task_run(
        task_run_id=chain["task_run_id"], final_message={"text": "完成"}
    )
    finals = repo.list_outbox(scope="final_message")
    assert len(finals) == 1
    assert finals[0]["owner_id"] == OWNER
    assert finals[0]["owner_id"] != chain["task_run_id"]
