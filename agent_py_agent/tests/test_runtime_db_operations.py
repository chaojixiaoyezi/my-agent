"""R2 权威操作测试（3.txt §5 测试 4-13 + F/G/H 不变量）。

覆盖：三层 fence（F.6/F.7，测试 9）、ToolOperation 状态机（G.1-G.5）、
资源锁（G.6-G.9，测试 11/12）、mutation 账（G.12-G.14）、PublishOperation
（H.2-H.9，测试 10/13）、root claim 规范化（测试 5/6）、binding 复用
（D.6，测试 4）、epoch 联动（F.1）。
"""

from __future__ import annotations

import json
import os
import platform
import sys
from pathlib import Path

import pytest

from agent_py_agent.agent.runtime_db.operations import (
    RESOURCE_VERSION_CONFLICT,
    OpaqueIdError,
    directory_id_for_opaque,
    sha256_of,
)
from agent_py_agent.agent.runtime_db.repository import (
    BINDING_ROOT_OVERLAP,
    ROOT_CLAIM_CONFLICT,
    RuntimeConflictError,
    RuntimeRepository,
)

OWNER = "local/main"


@pytest.fixture
def repo(tmp_path):
    return RuntimeRepository(tmp_path / "home" / "runtime.db")


@pytest.fixture
def ctx(repo):
    chain = repo.record_run_creation(owner_id=OWNER, run_id="run-main", goal="g")
    return chain, repo


# ------------------------------------------------------------------ 三层 fence
def test_fence_clean_pass(ctx):
    chain, repo = ctx
    attempt = repo.verify_fence(
        agent_run_id=chain["agent_run_id"], attempt_id=chain["attempt_id"]
    )
    assert attempt["attempt_id"] == chain["attempt_id"]


def test_fence_stale_attempt_rejected(ctx):
    chain, repo = ctx
    new = repo.create_attempt(chain["agent_run_id"])  # 接管：新 current
    with pytest.raises(RuntimeConflictError, match="已不是 current pointer"):
        repo.verify_fence(
            agent_run_id=chain["agent_run_id"], attempt_id=chain["attempt_id"]
        )
    # 新 attempt 放行。
    repo.verify_fence(agent_run_id=chain["agent_run_id"], attempt_id=new["attempt_id"])


def test_fence_epoch_mismatch_rejected(ctx, tmp_path):
    chain, repo = ctx
    binding = repo.create_binding(
        agent_run_id=chain["agent_run_id"],
        attempt_id=chain["attempt_id"],
        owner_id=OWNER,
        root_path=str(tmp_path / "work"),
    )
    repo.supersede_binding(binding_id=binding["binding_id"], expected_epoch=1)
    with pytest.raises(RuntimeConflictError, match="workspace_epoch"):
        repo.verify_fence(
            agent_run_id=chain["agent_run_id"],
            attempt_id=chain["attempt_id"],
            workspace_epoch=1,
        )


def test_fence_operation_generation_invalid(ctx):
    chain, repo = ctx
    with pytest.raises(RuntimeConflictError, match="tool_operation_generation"):
        repo.verify_fence(
            agent_run_id=chain["agent_run_id"],
            attempt_id=chain["attempt_id"],
            tool_operation_generation=0,
        )


def test_fence_operation_generation_must_match_own_row(ctx):
    """F.6：tool_operation_generation 必须等于操作行自己的 generation。"""
    chain, repo = ctx
    op = repo.create_tool_operation(
        agent_run_id=chain["agent_run_id"],
        attempt_id=chain["attempt_id"],
        operation_type="write",
    )
    row = repo.get_operation(op["operation_id"])
    own = int(row["tool_operation_generation"])
    assert own == 1
    # 代数 = 行自身值 → 放行。
    repo.verify_fence(
        agent_run_id=chain["agent_run_id"],
        attempt_id=chain["attempt_id"],
        tool_operation_id=op["operation_id"],
        tool_operation_generation=own,
    )
    # 代数 ≠ 行自身值 → 拒绝（拿错代数防不了旧操作重放）。
    with pytest.raises(RuntimeConflictError, match="≠ 行自身"):
        repo.verify_fence(
            agent_run_id=chain["agent_run_id"],
            attempt_id=chain["attempt_id"],
            tool_operation_id=op["operation_id"],
            tool_operation_generation=own + 1,
        )


def test_fence_operation_generation_requires_operation_id(ctx):
    """F.6：只有代数没有操作行 → 无从对照自身代数，拒绝凭空代数。"""
    chain, repo = ctx
    with pytest.raises(RuntimeConflictError, match="必须配 tool_operation_id"):
        repo.verify_fence(
            agent_run_id=chain["agent_run_id"],
            attempt_id=chain["attempt_id"],
            tool_operation_generation=3,
        )


def test_fence_unknown_operation_id_rejected(ctx):
    chain, repo = ctx
    with pytest.raises(RuntimeConflictError, match="tool_operation 不存在"):
        repo.verify_fence(
            agent_run_id=chain["agent_run_id"],
            attempt_id=chain["attempt_id"],
            tool_operation_id="tool_call:nope",
        )


def test_fence_operation_from_other_run_rejected(ctx):
    """F.6：操作行属于其它 run → 拒绝（跨 run 重放）。"""
    chain, repo = ctx
    other = repo.record_run_creation(owner_id=OWNER, run_id="run-other", goal="g2")
    op = repo.create_tool_operation(
        agent_run_id=other["agent_run_id"],
        attempt_id=other["attempt_id"],
        operation_type="write",
    )
    with pytest.raises(RuntimeConflictError, match="属于其它 run"):
        repo.verify_fence(
            agent_run_id=chain["agent_run_id"],
            attempt_id=chain["attempt_id"],
            tool_operation_id=op["operation_id"],
            tool_operation_generation=1,
        )


def test_fence_operation_stale_attempt_rejected(ctx):
    """F.6：操作行挂在旧 attempt（已接管）→ 拒绝（旧行不获权）。"""
    chain, repo = ctx
    op = repo.create_tool_operation(
        agent_run_id=chain["agent_run_id"],
        attempt_id=chain["attempt_id"],
        operation_type="write",
    )
    new = repo.create_attempt(chain["agent_run_id"])  # 接管：旧 attempt 下线
    with pytest.raises(RuntimeConflictError, match="已不是 current pointer"):
        repo.verify_fence(
            agent_run_id=chain["agent_run_id"],
            attempt_id=chain["attempt_id"],
            tool_operation_id=op["operation_id"],
            tool_operation_generation=1,
        )
    # 新 attempt 下同一操作行也不获权（行挂旧 attempt）。
    with pytest.raises(RuntimeConflictError, match="属于其它 attempt"):
        repo.verify_fence(
            agent_run_id=chain["agent_run_id"],
            attempt_id=new["attempt_id"],
            tool_operation_id=op["operation_id"],
            tool_operation_generation=1,
        )


def test_takeover_old_publish_rejected(ctx, tmp_path):
    """测试 9：takeover A 后旧 publish 拒绝（fence 在 publish 点重验）。"""
    chain, repo = ctx
    shared = tmp_path / "shared"
    staging = tmp_path / "staging"
    shared.mkdir()
    staging.mkdir()
    src = staging / "a.txt"
    src.write_text("v1", encoding="utf-8")
    binding = repo.create_binding(
        agent_run_id=chain["agent_run_id"],
        attempt_id=chain["attempt_id"],
        owner_id=OWNER,
        root_path=str(shared),
    )
    pub = repo.create_publish(
        binding_id=binding["binding_id"],
        agent_run_id=chain["agent_run_id"],
        attempt_id=chain["attempt_id"],
        workspace_epoch=1,
    )
    repo.stage_publish_manifest(
        pub["publish_id"],
        [{"path": "a.txt", "kind": "created", "postimage_digest": sha256_of(src)}],
    )
    # takeover：新 attempt 替换 current pointer。
    repo.create_attempt(chain["agent_run_id"])
    with pytest.raises(RuntimeConflictError, match="已不是 current pointer"):
        repo.publish(publish_id=pub["publish_id"], staging_root=staging, shared_root=shared)
    # 共享目录未被写入（fail-closed）。
    assert not (shared / "a.txt").exists()


def test_sibling_publish_unaffected(ctx, tmp_path):
    """测试 9：A 被 takeover，B/C 的 publish 正常。"""
    chain, repo = ctx
    shared = tmp_path / "shared"
    staging = tmp_path / "staging"
    shared.mkdir()
    staging.mkdir()
    pub_a = _staged_publish(repo, chain, shared, staging, "a.txt", "A")
    repo.create_attempt(chain["agent_run_id"])  # A 被接管
    with pytest.raises(RuntimeConflictError):
        repo.publish(publish_id=pub_a["publish_id"], staging_root=staging, shared_root=shared)
    # 兄弟 B 的 publish 不受影响。
    sibling = repo.record_run_creation(owner_id=OWNER, run_id="run-b", goal="b")
    binding_b = repo.create_binding(
        agent_run_id=sibling["agent_run_id"],
        attempt_id=sibling["attempt_id"],
        owner_id=OWNER,
        root_path=str(shared),
    )
    pub_b = repo.create_publish(
        binding_id=binding_b["binding_id"],
        agent_run_id=sibling["agent_run_id"],
        attempt_id=sibling["attempt_id"],
        workspace_epoch=1,
    )
    src_b = staging / "b.txt"
    src_b.write_text("B", encoding="utf-8")
    repo.stage_publish_manifest(
        pub_b["publish_id"],
        [{"path": "b.txt", "kind": "created", "postimage_digest": sha256_of(src_b)}],
    )
    done = repo.publish(publish_id=pub_b["publish_id"], staging_root=staging, shared_root=shared)
    assert done["status"] == "COMMITTED"
    assert (shared / "b.txt").read_text(encoding="utf-8") == "B"


# ----------------------------------------------------------------- ToolOperation
def test_operation_generation_increments(ctx):
    chain, repo = ctx
    for expected in (1, 2, 3):
        op = repo.create_tool_operation(
            agent_run_id=chain["agent_run_id"],
            attempt_id=chain["attempt_id"],
            operation_type="run_command",
        )
        assert op["tool_operation_generation"] == expected
        assert op["status"] == "CLAIMED"


def test_operation_requires_current_attempt(ctx):
    chain, repo = ctx
    old = repo.create_attempt(chain["agent_run_id"])  # 制造旧 attempt
    repo.create_attempt(chain["agent_run_id"])        # 新 current
    with pytest.raises(RuntimeConflictError, match="已不是 current pointer"):
        repo.create_tool_operation(
            agent_run_id=chain["agent_run_id"],
            attempt_id=old["attempt_id"],
            operation_type="write",
        )


def test_operation_executing_cas(ctx):
    chain, repo = ctx
    op = repo.create_tool_operation(
        agent_run_id=chain["agent_run_id"],
        attempt_id=chain["attempt_id"],
        operation_type="write",
    )
    executing = repo.mark_operation_executing(op["operation_id"])
    assert executing["status"] == "EXECUTING"
    assert executing["handler_started_at"] > 0
    with pytest.raises(RuntimeConflictError, match="无法进入 EXECUTING"):
        repo.mark_operation_executing(op["operation_id"])


def test_operation_settle_once(ctx):
    chain, repo = ctx
    op = repo.create_tool_operation(
        agent_run_id=chain["agent_run_id"],
        attempt_id=chain["attempt_id"],
        operation_type="write",
    )
    repo.mark_operation_executing(op["operation_id"])
    done = repo.settle_operation(op["operation_id"], "SUCCEEDED", {"ok": True})
    assert done["status"] == "SUCCEEDED" and done["settled_at"] > 0
    with pytest.raises(RuntimeConflictError, match="禁止二次 settle"):
        repo.settle_operation(op["operation_id"], "FAILED")


def test_settle_stale_attempt_rejected(ctx):
    """G4（用户 takeover 复现）：takeover 后旧 attempt 的操作不得结算。

    修复前：settle_operation 只查状态机（合法 outcome/未 settle），不重
    校验 current attempt/epoch/generation —— 旧 attempt 的操作可被结算
    SUCCEEDED（副作用无法归因到 current pointer）。现在 settle 前按操作
    行自身权威字段重验 fence（F.6 基准），失配 fail-closed。
    """
    chain, repo = ctx
    op = repo.create_tool_operation(
        agent_run_id=chain["agent_run_id"],
        attempt_id=chain["attempt_id"],
        operation_type="write",
    )
    repo.mark_operation_executing(op["operation_id"])
    repo.create_attempt(chain["agent_run_id"])  # takeover：推进 current pointer

    with pytest.raises(RuntimeConflictError, match="不是 current pointer"):
        repo.settle_operation(op["operation_id"], "SUCCEEDED")
    # G4 补 4：create_attempt 事务内 takeover 收尾已把旧 attempt 的非终态
    # 操作统一转 UNKNOWN（outcome_json.reason=takeover_recovery）——不留给
    # 事后 recovery 补标，旧 attempt 的操作在接管瞬间就是「结果不可知」。
    row = repo.get_operation(op["operation_id"])
    assert row["status"] == "UNKNOWN"
    assert json.loads(row["outcome_json"])["reason"] == "takeover_recovery"


def test_settle_rejected_after_epoch_advance(ctx):
    """G4：epoch 联动换代后旧操作同样不得结算（attempt generation 失配）。"""
    chain, repo = ctx
    op = repo.create_tool_operation(
        agent_run_id=chain["agent_run_id"],
        attempt_id=chain["attempt_id"],
        operation_type="write",
    )
    repo.mark_operation_executing(op["operation_id"])
    # 换代：新 attempt + epoch +1（F.1 联动）→ 旧操作结算被拒。
    new_attempt = repo.create_attempt(chain["agent_run_id"])
    assert int(new_attempt["attempt_generation"]) > 1
    with pytest.raises(RuntimeConflictError):
        repo.settle_operation(op["operation_id"], "FAILED")


def test_operation_cancelled_requires_not_started(ctx):
    chain, repo = ctx
    # 已启动的 handler 无法证明零副作用 → CANCELLED 拒绝（G.5）。
    started = repo.create_tool_operation(
        agent_run_id=chain["agent_run_id"],
        attempt_id=chain["attempt_id"],
        operation_type="run_command",
    )
    repo.mark_operation_executing(started["operation_id"])
    with pytest.raises(RuntimeConflictError, match="禁止 CANCELLED"):
        repo.settle_operation(started["operation_id"], "CANCELLED")
    # 未启动（仅 CLAIMED）→ 可 CANCELLED。
    idle = repo.create_tool_operation(
        agent_run_id=chain["agent_run_id"],
        attempt_id=chain["attempt_id"],
        operation_type="run_command",
    )
    cancelled = repo.settle_operation(idle["operation_id"], "CANCELLED", {"reason": "cancel"})
    assert cancelled["status"] == "CANCELLED"


def test_operation_unknown(ctx):
    chain, repo = ctx
    op = repo.create_tool_operation(
        agent_run_id=chain["agent_run_id"],
        attempt_id=chain["attempt_id"],
        operation_type="run_command",
    )
    repo.mark_operation_executing(op["operation_id"])
    unknown = repo.mark_operation_unknown(op["operation_id"], "owner 消失")
    assert unknown["status"] == "UNKNOWN"


def test_operation_reopen_only_claimed(ctx):
    chain, repo = ctx
    idle = repo.create_tool_operation(
        agent_run_id=chain["agent_run_id"],
        attempt_id=chain["attempt_id"],
        operation_type="write",
    )
    assert repo.can_reopen_operation(idle["operation_id"])
    repo.mark_operation_executing(idle["operation_id"])
    assert not repo.can_reopen_operation(idle["operation_id"])


# ------------------------------------------------- G4 补（3.txt G4-2/3/4）
def test_g4_mark_operation_atomic_start_gate(ctx):
    """G4 补 3：原子 start 门——create 与 mark 之间发生 takeover 也无法把
    旧 attempt 的操作置 EXECUTING。修复前 verify_fence 与 mark 是两步，
    两步之间被推进 pointer 则 verify 已过、mark 却落进死 attempt。现在
    current-pointer 条件并入同一 UPDATE，零窗口。
    """
    chain, repo = ctx
    op = repo.create_tool_operation(
        agent_run_id=chain["agent_run_id"],
        attempt_id=chain["attempt_id"],
        operation_type="write",
    )
    repo.create_attempt(chain["agent_run_id"])  # 接管：旧 attempt 失去执行权
    with pytest.raises(RuntimeConflictError, match="无法进入 EXECUTING"):
        repo.mark_operation_executing(
            op["operation_id"],
            agent_run_id=chain["agent_run_id"],
            attempt_id=chain["attempt_id"],
            tool_operation_generation=int(op["tool_operation_generation"]),
        )
    # 原子门拒绝 → 操作行停留在接管收尾状态（UNKNOWN，见 G4 补 4）。
    assert repo.get_operation(op["operation_id"])["status"] == "UNKNOWN"


def test_g4_mark_atomic_gate_passes_on_current_attempt(ctx):
    """G4 补 3 正向：带 fence 参数在主链 current attempt 上正常置 EXECUTING。"""
    chain, repo = ctx
    op = repo.create_tool_operation(
        agent_run_id=chain["agent_run_id"],
        attempt_id=chain["attempt_id"],
        operation_type="write",
    )
    executing = repo.mark_operation_executing(
        op["operation_id"],
        agent_run_id=chain["agent_run_id"],
        attempt_id=chain["attempt_id"],
        tool_operation_generation=int(op["tool_operation_generation"]),
    )
    assert executing["status"] == "EXECUTING"
    assert executing["handler_started_at"] > 0


def test_g4_create_attempt_takeover_cleanup(ctx):
    """G4 补 4：takeover 以系统权威统一收尾旧 attempt——非终态操作
    CLAIMED/EXECUTING → UNKNOWN(reason=takeover_recovery)，进行中
    mutation（MUTATING）→ DIRTY(reason=takeover_recovery)，旧 attempt
    生命周期 → cancelled，并留下 superseded 事件。
    同一事务（INSERT attempt + CAS current pointer + 收尾 + commit），
    接管瞬间旧 attempt 的「结果不可知」即落账，不留给事后 recovery。
    """
    chain, repo = ctx
    idle = repo.create_tool_operation(
        agent_run_id=chain["agent_run_id"],
        attempt_id=chain["attempt_id"],
        operation_type="write",
    )
    running = repo.create_tool_operation(
        agent_run_id=chain["agent_run_id"],
        attempt_id=chain["attempt_id"],
        operation_type="run_command",
    )
    repo.mark_operation_executing(running["operation_id"])
    repo.begin_mutation(canonical_scope="scope:a", attempt_id=chain["attempt_id"])

    new_attempt = repo.create_attempt(chain["agent_run_id"])  # takeover

    for op_id in (idle["operation_id"], running["operation_id"]):
        row = repo.get_operation(op_id)
        assert row["status"] == "UNKNOWN"
        assert json.loads(row["outcome_json"])["reason"] == "takeover_recovery"
    mutation = repo.mutation_for_scope("scope:a")
    assert mutation["state"] == "DIRTY"
    assert mutation["dirty_reason"] == "takeover_recovery"
    old_attempt = repo.get_attempt(chain["attempt_id"])
    assert old_attempt["status"] == "cancelled"
    assert float(old_attempt["ended_at"]) > 0
    superseded = [
        event
        for event in repo.events_for_attempt(chain["attempt_id"])
        if event["event_type"] == "agent_attempt.superseded"
    ]
    assert superseded[-1]["payload"] == {
        "previous_status": "running",
        "status": "cancelled",
        "reason": "takeover_recovery",
        "superseded_by_attempt_id": new_attempt["attempt_id"],
        "superseded_by_generation": int(new_attempt["attempt_generation"]),
    }
    # 新 attempt 不受接管收尾污染：fence 可过、可正常建操作。
    fresh = repo.create_tool_operation(
        agent_run_id=chain["agent_run_id"],
        attempt_id=new_attempt["attempt_id"],
        operation_type="write",
    )
    assert fresh["status"] == "CLAIMED"


def test_g4_create_attempt_repairs_legacy_noncurrent_active_attempts(ctx):
    """换代时一并收口升级前遗留的非 current RUNNING，账本只留一个 active。"""
    chain, repo = ctx
    second = repo.create_attempt(chain["agent_run_id"])
    with repo.transaction() as conn:
        conn.execute(
            "UPDATE agent_attempts SET status = 'running', ended_at = 0 "
            "WHERE attempt_id = ?",
            (chain["attempt_id"],),
        )

    third = repo.create_attempt(chain["agent_run_id"])

    with repo._runtime_connection() as conn:
        active = conn.execute(
            "SELECT attempt_id FROM agent_attempts WHERE agent_run_id = ? "
            "AND status IN ('pending', 'running') AND ended_at = 0",
            (chain["agent_run_id"],),
        ).fetchall()
    assert [str(row["attempt_id"]) for row in active] == [third["attempt_id"]]
    assert repo.get_attempt(chain["attempt_id"])["status"] == "cancelled"
    assert repo.get_attempt(second["attempt_id"])["status"] == "cancelled"


def test_g4_periodic_reconcile_repairs_legacy_noncurrent_active_attempt(ctx):
    """owner 周期巡检无需再挂新一轮，也能迁移升级前的幽灵 RUNNING。"""
    chain, repo = ctx
    current = repo.create_attempt(chain["agent_run_id"])
    with repo.transaction() as conn:
        conn.execute(
            "UPDATE agent_attempts SET status = 'running', ended_at = 0 "
            "WHERE attempt_id = ?",
            (chain["attempt_id"],),
        )

    reconciled = repo.reconcile_superseded_attempts(now=12345.0)

    assert reconciled == [chain["attempt_id"]]
    stale = repo.get_attempt(chain["attempt_id"])
    assert stale["status"] == "cancelled"
    assert float(stale["ended_at"]) == 12345.0
    assert repo.get_attempt(current["attempt_id"])["status"] == "running"
    events = repo.events_for_attempt(chain["attempt_id"])
    assert events[-1]["event_type"] == "agent_attempt.superseded"
    assert events[-1]["payload"]["reason"] == "stale_noncurrent_reconcile"


# ------------------------------------------------------------------- 资源锁
def test_acquire_sorted_all_or_nothing(ctx):
    chain, repo = ctx
    holder = "proc-1"
    locks = repo.acquire_locks(
        ["b", "a"],
        holder_instance=holder,
        attempt_id=chain["attempt_id"],
        attempt_generation=1,
        workspace_epoch=1,
    )
    assert [lock["canonical_scope"] for lock in locks] == ["a", "b"]
    # 反向申请同一资源 → 冲突且一个都不取（事务回滚）。
    with pytest.raises(RuntimeConflictError, match="资源锁冲突"):
        repo.acquire_locks(
            ["a", "c"],
            holder_instance="proc-2",
            attempt_id=chain["attempt_id"],
            attempt_generation=1,
            workspace_epoch=1,
        )
    assert repo.lock_for_scope("c") is None


def test_acquire_reverse_order_no_deadlock(ctx):
    """测试 11：多资源反向申请不死锁——排序后路径一致，冲突即明确拒绝。"""
    chain, repo = ctx
    holder = "proc-1"
    repo.acquire_locks(
        ["x", "y"],
        holder_instance=holder,
        attempt_id=chain["attempt_id"],
        attempt_generation=1,
        workspace_epoch=1,
    )
    # 反向顺序申请相同集合：已全部持有者身份一致则直接冲突（无等待，不死锁）。
    with pytest.raises(RuntimeConflictError):
        repo.acquire_locks(
            ["y", "x"],
            holder_instance="proc-2",
            attempt_id=chain["attempt_id"],
            attempt_generation=1,
            workspace_epoch=1,
        )
    # 释放后反向申请成功。
    assert repo.release_locks(["y", "x"], holder_instance=holder) == 2
    locks = repo.acquire_locks(
        ["y", "x"],
        holder_instance="proc-2",
        attempt_id=chain["attempt_id"],
        attempt_generation=1,
        workspace_epoch=1,
    )
    assert [lock["canonical_scope"] for lock in locks] == ["x", "y"]


def test_renew_cas(ctx):
    chain, repo = ctx
    repo.acquire_locks(
        ["w"],
        holder_instance="proc-1",
        attempt_id=chain["attempt_id"],
        attempt_generation=1,
        workspace_epoch=1,
        lease_seconds=1,
    )
    renewed = repo.renew_lock(
        canonical_scope="w",
        holder_instance="proc-1",
        attempt_id=chain["attempt_id"],
        attempt_generation=1,
        lease_seconds=60,
    )
    assert renewed["lease_expires_at"] > 0
    with pytest.raises(RuntimeConflictError, match="renew"):
        repo.renew_lock(
            canonical_scope="w",
            holder_instance="proc-2",  # 非持有者
            attempt_id=chain["attempt_id"],
            attempt_generation=1,
        )


def test_renew_after_lost_current_rejected(ctx):
    chain, repo = ctx
    repo.acquire_locks(
        ["w"],
        holder_instance="proc-1",
        attempt_id=chain["attempt_id"],
        attempt_generation=1,
        workspace_epoch=1,
    )
    repo.create_attempt(chain["agent_run_id"])  # 旧 attempt 失去 current pointer
    with pytest.raises(RuntimeConflictError, match="失去 current pointer"):
        repo.renew_lock(
            canonical_scope="w",
            holder_instance="proc-1",
            attempt_id=chain["attempt_id"],
            attempt_generation=1,
        )


def test_expired_lock_not_handed_to_second_writer(ctx):
    """测试 12：lease 过期但旧进程可能存活 → 不直接交给第二 writer。"""
    chain, repo = ctx
    repo.acquire_locks(
        ["w"],
        holder_instance="proc-1",
        attempt_id=chain["attempt_id"],
        attempt_generation=1,
        workspace_epoch=1,
        lease_seconds=1,
    )
    expired = repo.expired_locks(now=10**18)  # 模拟未来：lease 已过期
    assert [lock["canonical_scope"] for lock in expired] == ["w"]
    # 过期 ≠ 可抢占：第二 writer 仍被 UNIQUE 拒绝，须先对账/终止旧持有者。
    with pytest.raises(RuntimeConflictError, match="资源锁冲突"):
        repo.acquire_locks(
            ["w"],
            holder_instance="proc-2",
            attempt_id=chain["attempt_id"],
            attempt_generation=1,
            workspace_epoch=1,
        )


def test_release_idempotent(ctx):
    chain, repo = ctx
    repo.acquire_locks(
        ["w"],
        holder_instance="proc-1",
        attempt_id=chain["attempt_id"],
        attempt_generation=1,
        workspace_epoch=1,
    )
    assert repo.release_locks(["w"], holder_instance="proc-1") == 1
    assert repo.release_locks(["w"], holder_instance="proc-1") == 0


# ------------------------------------------------------------------- mutation
def test_mutation_cycle(ctx):
    chain, repo = ctx
    repo.begin_mutation(canonical_scope="w", attempt_id=chain["attempt_id"])
    assert repo.mutation_for_scope("w")["state"] == "MUTATING"
    repo.mark_mutation_stable("w")
    assert repo.mutation_for_scope("w")["state"] == "STABLE"


def test_mutation_begin_conflict(ctx):
    chain, repo = ctx
    repo.begin_mutation(canonical_scope="w", attempt_id=chain["attempt_id"])
    with pytest.raises(RuntimeConflictError, match="非 STABLE"):
        repo.begin_mutation(canonical_scope="w", attempt_id=chain["attempt_id"])


def test_dirty_blocks_publish(ctx, tmp_path):
    """G.14：unknown/unscoped 写置 DIRTY → 阻止发布/验收/交付。"""
    chain, repo = ctx
    repo.mark_mutation_dirty(
        canonical_scope="", reason="unscoped shell write", attempt_id=chain["attempt_id"]
    )
    assert [r["canonical_scope"] for r in repo.workspace_dirty()] == [""]
    shared = tmp_path / "shared"
    staging = tmp_path / "staging"
    shared.mkdir()
    staging.mkdir()
    pub = _staged_publish(repo, chain, shared, staging, "a.txt", "v1")
    with pytest.raises(RuntimeConflictError, match="DIRTY mutation"):
        repo.publish(publish_id=pub["publish_id"], staging_root=staging, shared_root=shared)


# -------------------------------------------------------------------- publish
def _staged_publish(repo, chain, shared, staging, rel, content):
    src = staging / rel
    src.parent.mkdir(parents=True, exist_ok=True)
    src.write_text(content, encoding="utf-8")
    binding = repo.create_binding(
        agent_run_id=chain["agent_run_id"],
        attempt_id=chain["attempt_id"],
        owner_id=OWNER,
        root_path=str(shared),
    )
    pub = repo.create_publish(
        binding_id=binding["binding_id"],
        agent_run_id=chain["agent_run_id"],
        attempt_id=chain["attempt_id"],
        workspace_epoch=1,
    )
    repo.stage_publish_manifest(
        pub["publish_id"],
        [{"path": rel, "kind": "created", "postimage_digest": sha256_of(src)}],
    )
    return pub


def test_publish_happy_path(ctx, tmp_path):
    """H.5-H.8：staging→共享原子发布，完成后才写 ArtifactRecord。"""
    chain, repo = ctx
    shared = tmp_path / "shared"
    staging = tmp_path / "staging"
    shared.mkdir()
    staging.mkdir()
    pub = _staged_publish(repo, chain, shared, staging, "a.txt", "hello")
    assert pub["status"] == "STAGING"
    done = repo.publish(publish_id=pub["publish_id"], staging_root=staging, shared_root=shared)
    assert done["status"] == "COMMITTED"
    assert done["committed_at"] > 0
    assert (shared / "a.txt").read_text(encoding="utf-8") == "hello"
    artifacts = repo.artifacts_for_attempt(chain["attempt_id"])
    assert [a["rel_path"] for a in artifacts] == ["a.txt"]
    assert artifacts[0]["content_digest"] == sha256_of(shared / "a.txt")
    assert artifacts[0]["content_path"]  # G2：内容寻址落盘，validator 只读该对象
    assert Path(artifacts[0]["content_path"]).read_text(encoding="utf-8") == "hello"


@pytest.mark.parametrize(
    "evil_path",
    [
        "../outside.txt",
        "a/../../outside.txt",
        "../../etc/passwd",
        "a/../b.txt",
        "/abs/path.txt",
        "a//b.txt",
        "a\\b.txt",
        "a/./b.txt",
        "a/\tb.txt",
    ],
)
def test_publish_manifest_rejects_escaping_paths(ctx, evil_path):
    """H.3/B.2：manifest 路径拒绝绝对路径、点段、反斜杠与控制字符（发布逃逸）。"""
    chain, repo = ctx
    pub = repo.create_publish(
        binding_id="binding-1",
        agent_run_id=chain["agent_run_id"],
        attempt_id=chain["attempt_id"],
        workspace_epoch=1,
    )
    with pytest.raises(RuntimeConflictError, match="manifest 路径非法"):
        repo.stage_publish_manifest(
            pub["publish_id"],
            [{"path": evil_path, "kind": "updated"}],
        )


def test_publish_manifest_accepts_normal_relative_paths(ctx):
    chain, repo = ctx
    pub = repo.create_publish(
        binding_id="binding-1",
        agent_run_id=chain["agent_run_id"],
        attempt_id=chain["attempt_id"],
        workspace_epoch=1,
    )
    repo.stage_publish_manifest(
        pub["publish_id"],
        [
            {"path": "output/report.md", "kind": "updated"},
            {"path": "src/pkg/mod.go", "kind": "created"},
        ],
    )
    row = repo.get_publish(pub["publish_id"])
    assert row["manifest_json"] != "[]"


def test_publish_rejects_symlink_parent_escape(ctx, tmp_path):
    """G3（探针 symlink_publish_wrote_outside 复现）：共享根下 symlink
    目录把发布写出根外 —— apply 的 os.replace 沿中间组件 symlink 出根。

    修复前：stage 只有字符串级路径检查（H.3），apply 前无文件系统级
    resolve/symlink 校验 —— manifest 声明 link/evil.txt（link → 根外），
    preimage 校验与 os.replace 都会写/读根外。
    """
    chain, repo = ctx
    shared = tmp_path / "shared"
    staging = tmp_path / "staging"
    outside = tmp_path / "outside"
    shared.mkdir()
    staging.mkdir()
    outside.mkdir()
    (outside / "evil.txt").write_text("v1", encoding="utf-8")
    (shared / "link").symlink_to(outside, target_is_directory=True)
    # preimage digest 匹配根外文件 → 若没有 symlink 防线，publish 会把它
    # 替换成 staging 内容（越界写）。
    src = staging / "link" / "evil.txt"
    src.parent.mkdir(parents=True, exist_ok=True)
    src.write_text("evil-payload", encoding="utf-8")
    pub = repo.create_publish(
        binding_id="binding-1",
        agent_run_id=chain["agent_run_id"],
        attempt_id=chain["attempt_id"],
        workspace_epoch=1,
    )
    repo.stage_publish_manifest(
        pub["publish_id"],
        [{"path": "link/evil.txt", "kind": "updated", "preimage_digest": sha256_of(outside / "evil.txt")}],
    )
    with pytest.raises(RuntimeConflictError, match="symlink"):
        repo.publish(publish_id=pub["publish_id"], staging_root=staging, shared_root=shared)
    # 根外文件原封未动，publish 保持 STAGING（零副作用）。
    assert (outside / "evil.txt").read_text(encoding="utf-8") == "v1"
    assert repo.get_publish(pub["publish_id"])["status"] == "STAGING"


def test_publish_rejects_db_tampered_dotdot_path(ctx, tmp_path):
    """G3：DB 直改 manifest 绕开 stage 字符串检查 → 执行级 resolve 根包含兜底。

    stage 的 _validate_manifest_rel_path 只在写入时拦字符串形态；manifest
    存库后被人为改写（本地权威库被篡改）时，apply 前的 resolve 检查必须
    独立再拦一次（B.2 拒绝式，不依赖存量数据可信）。
    """
    import sqlite3

    chain, repo = ctx
    shared = tmp_path / "shared"
    staging = tmp_path / "staging"
    shared.mkdir()
    staging.mkdir()
    pub = repo.create_publish(
        binding_id="binding-1",
        agent_run_id=chain["agent_run_id"],
        attempt_id=chain["attempt_id"],
        workspace_epoch=1,
    )
    repo.stage_publish_manifest(
        pub["publish_id"],
        [{"path": "a.txt", "kind": "created", "postimage_digest": "d" * 64}],
    )
    # 直改 manifest_json：注入逃逸路径。
    import json

    tampered = json.dumps(
        [{"path": "../escape.txt", "kind": "created", "postimage_digest": "d" * 64}]
    )
    with sqlite3.connect(str(repo.db_path)) as conn:
        conn.execute(
            "UPDATE publish_operations SET manifest_json = ? WHERE publish_id = ?",
            (tampered, pub["publish_id"]),
        )
        conn.commit()
    with pytest.raises(RuntimeConflictError, match="逃逸"):
        repo.publish(publish_id=pub["publish_id"], staging_root=staging, shared_root=shared)
    assert not (tmp_path / "escape.txt").exists()
    assert not (shared / "escape.txt").exists()


def test_publish_rejects_staging_symlink_escape(ctx, tmp_path):
    """G3：staging 侧 symlink 同样拒绝 —— os.replace 移动的是链接本身，
    共享区会得到指向根外的 symlink（内容寻址断裂：digest 记账与实际
    内容不符）。
    """
    chain, repo = ctx
    shared = tmp_path / "shared"
    staging = tmp_path / "staging"
    outside = tmp_path / "outside"
    shared.mkdir()
    staging.mkdir()
    outside.mkdir()
    (outside / "evil.txt").write_text("payload", encoding="utf-8")
    (staging / "link").symlink_to(outside, target_is_directory=True)
    (staging / "link" / "evil.txt").write_text("real", encoding="utf-8")
    pub = repo.create_publish(
        binding_id="binding-1",
        agent_run_id=chain["agent_run_id"],
        attempt_id=chain["attempt_id"],
        workspace_epoch=1,
    )
    repo.stage_publish_manifest(
        pub["publish_id"],
        [{"path": "link/evil.txt", "kind": "created", "postimage_digest": sha256_of(staging / "link" / "evil.txt")}],
    )
    with pytest.raises(RuntimeConflictError, match="symlink"):
        repo.publish(publish_id=pub["publish_id"], staging_root=staging, shared_root=shared)
    assert repo.get_publish(pub["publish_id"])["status"] == "STAGING"


def test_publish_preimage_conflict(ctx, tmp_path):
    """测试 10：同资源同 preimage 恰一发布，另一争用方 RESOURCE_VERSION_CONFLICT。"""
    chain, repo = ctx
    shared = tmp_path / "shared"
    staging = tmp_path / "staging"
    shared.mkdir()
    staging.mkdir()
    # 第一个 publish：staging v1 → 共享 v1。
    first = _staged_publish(repo, chain, shared, staging, "a.txt", "v1")
    repo.publish(publish_id=first["publish_id"], staging_root=staging, shared_root=shared)
    # 第二个 publish 声明同样 preimage（v1 的 digest）但共享已是 v1——
    # preimage 匹配 → 恰好一次提交；再试一次（共享仍是 v1,preimage 仍匹配）
    # 应正常（幂等内容）。构造真正的冲突：第二个 publish 用旧 digest 声明。
    stale = staging / "a.txt"
    stale.write_text("stale-preimage", encoding="utf-8")
    binding = repo.create_binding(
        agent_run_id=chain["agent_run_id"],
        attempt_id=chain["attempt_id"],
        owner_id=OWNER,
        root_path=str(shared),
    )
    pub2 = repo.create_publish(
        binding_id=binding["binding_id"],
        agent_run_id=chain["agent_run_id"],
        attempt_id=chain["attempt_id"],
        workspace_epoch=1,
    )
    # 声明 preimage = 一个不存在的旧版本 digest → 与共享现状失配 → 冲突。
    repo.stage_publish_manifest(
        pub2["publish_id"],
        [{
            "path": "a.txt",
            "kind": "updated",
            "preimage_digest": "f" * 64,
            "postimage_digest": sha256_of(stale),
        }],
    )
    with pytest.raises(RuntimeConflictError, match=RESOURCE_VERSION_CONFLICT):
        repo.publish(publish_id=pub2["publish_id"], staging_root=staging, shared_root=shared)


def test_publish_created_conflict(ctx, tmp_path):
    chain, repo = ctx
    shared = tmp_path / "shared"
    staging = tmp_path / "staging"
    shared.mkdir()
    staging.mkdir()
    (shared / "a.txt").write_text("exists", encoding="utf-8")
    pub = _staged_publish(repo, chain, shared, staging, "a.txt", "v1")
    with pytest.raises(RuntimeConflictError, match=RESOURCE_VERSION_CONFLICT):
        repo.publish(publish_id=pub["publish_id"], staging_root=staging, shared_root=shared)


def test_publish_deleted(ctx, tmp_path):
    chain, repo = ctx
    shared = tmp_path / "shared"
    staging = tmp_path / "staging"
    shared.mkdir()
    staging.mkdir()
    target = shared / "old.txt"
    target.write_text("bye", encoding="utf-8")
    binding = repo.create_binding(
        agent_run_id=chain["agent_run_id"],
        attempt_id=chain["attempt_id"],
        owner_id=OWNER,
        root_path=str(shared),
    )
    pub = repo.create_publish(
        binding_id=binding["binding_id"],
        agent_run_id=chain["agent_run_id"],
        attempt_id=chain["attempt_id"],
        workspace_epoch=1,
    )
    repo.stage_publish_manifest(
        pub["publish_id"],
        [{"path": "old.txt", "kind": "deleted", "preimage_digest": sha256_of(target)}],
    )
    done = repo.publish(publish_id=pub["publish_id"], staging_root=staging, shared_root=shared)
    assert done["status"] == "COMMITTED"
    assert not target.exists()


def test_publish_rejects_terminal_symlink_created(ctx, tmp_path):
    """G3 补：created 项末段是 symlink → CAS 探测 openat(O_NOFOLLOW)
    ELOOP 拒绝。修复前 target.exists() 跟随链接判「已存在」也能拦，但
    仅靠路径级检查存在检查与 apply 之间的替换窗口；fd 层 O_NOFOLLOW
    把探测与 apply 绑到同一父 fd，替换无隙可乘。"""
    chain, repo = ctx
    shared = tmp_path / "shared"
    staging = tmp_path / "staging"
    shared.mkdir()
    staging.mkdir()
    real = tmp_path / "real.txt"
    real.write_text("outside", encoding="utf-8")
    (shared / "a.txt").symlink_to(real)
    (staging / "a.txt").write_text("new", encoding="utf-8")  # CAS 在 preimage 前拒绝,不会读到它
    binding = repo.create_binding(
        agent_run_id=chain["agent_run_id"],
        attempt_id=chain["attempt_id"],
        owner_id=OWNER,
        root_path=str(shared),
    )
    pub = repo.create_publish(
        binding_id=binding["binding_id"],
        agent_run_id=chain["agent_run_id"],
        attempt_id=chain["attempt_id"],
        workspace_epoch=1,
    )
    repo.stage_publish_manifest(
        pub["publish_id"],
        [{"path": "a.txt", "kind": "created",
          "postimage_digest": sha256_of(staging / "a.txt")}],
    )
    with pytest.raises(RuntimeConflictError, match="symlink"):
        repo.publish(publish_id=pub["publish_id"], staging_root=staging, shared_root=shared)
    assert repo.get_publish(pub["publish_id"])["status"] == "STAGING"  # fail-closed 零副作用
    assert (shared / "a.txt").is_symlink()  # 链接未被跟随/替换


def test_publish_rejects_terminal_symlink_updated(ctx, tmp_path):
    """G3 补：updated 项末段是 symlink（指向根外）→ preimage CAS
    openat(O_NOFOLLOW) ELOOP 拒绝。修复前 sha256_of 跟随链接把根外
    文件内容当 preimage 比对（CAS 可通过），TOCTOU 面在读取与替换之间；
    fd 层探测即拒绝，外部文件连读都不发生。"""
    chain, repo = ctx
    shared = tmp_path / "shared"
    staging = tmp_path / "staging"
    shared.mkdir()
    staging.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("secret", encoding="utf-8")
    (shared / "a.txt").symlink_to(outside)
    (staging / "a.txt").write_text("new", encoding="utf-8")
    binding = repo.create_binding(
        agent_run_id=chain["agent_run_id"],
        attempt_id=chain["attempt_id"],
        owner_id=OWNER,
        root_path=str(shared),
    )
    pub = repo.create_publish(
        binding_id=binding["binding_id"],
        agent_run_id=chain["agent_run_id"],
        attempt_id=chain["attempt_id"],
        workspace_epoch=1,
    )
    repo.stage_publish_manifest(
        pub["publish_id"],
        [{"path": "a.txt", "kind": "updated", "preimage_digest": sha256_of(outside)}],
    )
    with pytest.raises(RuntimeConflictError, match="symlink"):
        repo.publish(publish_id=pub["publish_id"], staging_root=staging, shared_root=shared)
    assert outside.read_text(encoding="utf-8") == "secret"  # 外部未被写入
    assert (shared / "a.txt").is_symlink()  # 链接未被替换


def test_publish_rejects_terminal_symlink_deleted(ctx, tmp_path):
    """G3 补：deleted 项末段是 symlink → CAS ELOOP 拒绝。修复前
    is_file() 跟随链接判存在、unlink 只删链接（不逃逸但 preimage 校验
    读了外部）；fd 层 fail-closed：链接既不读也不删。"""
    chain, repo = ctx
    shared = tmp_path / "shared"
    staging = tmp_path / "staging"
    shared.mkdir()
    staging.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("secret", encoding="utf-8")
    link = shared / "a.txt"
    link.symlink_to(outside)
    binding = repo.create_binding(
        agent_run_id=chain["agent_run_id"],
        attempt_id=chain["attempt_id"],
        owner_id=OWNER,
        root_path=str(shared),
    )
    pub = repo.create_publish(
        binding_id=binding["binding_id"],
        agent_run_id=chain["agent_run_id"],
        attempt_id=chain["attempt_id"],
        workspace_epoch=1,
    )
    repo.stage_publish_manifest(
        pub["publish_id"],
        [{"path": "a.txt", "kind": "deleted", "preimage_digest": sha256_of(outside)}],
    )
    with pytest.raises(RuntimeConflictError, match="symlink"):
        repo.publish(publish_id=pub["publish_id"], staging_root=staging, shared_root=shared)
    assert link.is_symlink()  # 链接未被删


def test_publish_creates_nested_parent_dirs(ctx, tmp_path):
    """G3 补：created 项目标父目录不存在 → fd-relative 逐段 mkdirat
    创建（O_NOFOLLOW 保证中间段无 symlink），发布成功。"""
    chain, repo = ctx
    shared = tmp_path / "shared"
    staging = tmp_path / "staging"
    shared.mkdir()
    staging.mkdir()
    pub = _staged_publish(repo, chain, shared, staging, "a/b/c.txt", "deep")
    done = repo.publish(publish_id=pub["publish_id"], staging_root=staging, shared_root=shared)
    assert done["status"] == "COMMITTED"
    assert (shared / "a" / "b" / "c.txt").read_text(encoding="utf-8") == "deep"


def test_publish_applies_permissions_fchmod(ctx, tmp_path):
    """G3 补：permissions 项 fd-relative 后经 openat+O_NOFOLLOW 打开再
    fchmod（路径不重解析），权限仍生效。"""
    chain, repo = ctx
    shared = tmp_path / "shared"
    staging = tmp_path / "staging"
    shared.mkdir()
    staging.mkdir()
    src = staging / "a.txt"
    src.write_text("hi", encoding="utf-8")
    binding = repo.create_binding(
        agent_run_id=chain["agent_run_id"],
        attempt_id=chain["attempt_id"],
        owner_id=OWNER,
        root_path=str(shared),
    )
    pub = repo.create_publish(
        binding_id=binding["binding_id"],
        agent_run_id=chain["agent_run_id"],
        attempt_id=chain["attempt_id"],
        workspace_epoch=1,
    )
    repo.stage_publish_manifest(
        pub["publish_id"],
        [{"path": "a.txt", "kind": "created", "postimage_digest": sha256_of(src),
          "permissions": 0o640}],
    )
    done = repo.publish(publish_id=pub["publish_id"], staging_root=staging, shared_root=shared)
    assert done["status"] == "COMMITTED"
    assert (shared / "a.txt").stat().st_mode & 0o777 == 0o640


@pytest.mark.parametrize("crash_point", ["after_fence", "after_preimage", "mid_apply"])
def test_publish_crash_points_land_dirty(ctx, tmp_path, crash_point):
    """测试 13：publish 任一 crash point 只得到 COMMITTED 或 DIRTY/UNKNOWN。"""
    chain, repo = ctx
    shared = tmp_path / "shared"
    staging = tmp_path / "staging"
    shared.mkdir()
    staging.mkdir()
    pub = _staged_publish(repo, chain, shared, staging, "a.txt", "v1")
    result = repo.publish(
        publish_id=pub["publish_id"],
        staging_root=staging,
        shared_root=shared,
        crash_point=crash_point,
    )
    assert result["status"] in {"COMMITTED", "DIRTY", "UNKNOWN"}
    if crash_point == "after_fence":
        # fence 后立即崩 → 共享未动。
        assert not (shared / "a.txt").exists()


def test_publish_mid_apply_partial_files(ctx, tmp_path):
    """mid_apply 崩溃：首个文件已写入，其余未动，状态 DIRTY（H.7 不谎报）。"""
    chain, repo = ctx
    shared = tmp_path / "shared"
    staging = tmp_path / "staging"
    shared.mkdir()
    staging.mkdir()
    for name, content in (("one.txt", "1"), ("two.txt", "2")):
        (staging / name).write_text(content, encoding="utf-8")
    binding = repo.create_binding(
        agent_run_id=chain["agent_run_id"],
        attempt_id=chain["attempt_id"],
        owner_id=OWNER,
        root_path=str(shared),
    )
    pub = repo.create_publish(
        binding_id=binding["binding_id"],
        agent_run_id=chain["agent_run_id"],
        attempt_id=chain["attempt_id"],
        workspace_epoch=1,
    )
    repo.stage_publish_manifest(
        pub["publish_id"],
        [
            {"path": "one.txt", "kind": "created", "postimage_digest": sha256_of(staging / "one.txt")},
            {"path": "two.txt", "kind": "created", "postimage_digest": sha256_of(staging / "two.txt")},
        ],
    )
    result = repo.publish(
        publish_id=pub["publish_id"],
        staging_root=staging,
        shared_root=shared,
        crash_point="mid_apply",
    )
    assert result["status"] == "DIRTY"
    assert (shared / "one.txt").exists()          # 第一个已 apply
    assert not (shared / "two.txt").exists()      # 第二个未动
    assert not (shared / "one.txt").is_symlink()


# ------------------------------------------------------------ claim/binding
def test_binding_reuse_same_digest(ctx, tmp_path):
    """测试 4：相同规范化 root set（digest）并发创建只得到一个 ACTIVE binding。"""
    chain, repo = ctx
    first = repo.create_binding(
        agent_run_id=chain["agent_run_id"],
        attempt_id=chain["attempt_id"],
        owner_id=OWNER,
        root_path=str(tmp_path / "work"),
        writable_roots=["/x/b", "/x/a"],
        roots_digest="digest-1",
    )
    second = repo.create_binding(
        agent_run_id=chain["agent_run_id"],
        attempt_id=chain["attempt_id"],
        owner_id=OWNER,
        root_path=str(tmp_path / "work"),
        writable_roots=["/x/a", "/x/b"],  # 顺序无关
        roots_digest="digest-1",
    )
    assert second["binding_id"] == first["binding_id"]
    assert second["status"] == "ACTIVE"


def test_claim_parent_child_overlap(ctx, tmp_path):
    """测试 5：父子路径别名冲突拒绝。"""
    chain, repo = ctx
    repo.claim_root(
        binding_id="b1",
        agent_run_id=chain["agent_run_id"],
        attempt_id=chain["attempt_id"],
        root_path=str(tmp_path / "work"),
    )
    with pytest.raises(RuntimeConflictError, match=ROOT_CLAIM_CONFLICT):
        repo.claim_root(
            binding_id="b2",
            agent_run_id=chain["agent_run_id"],
            attempt_id=chain["attempt_id"],
            root_path=str(tmp_path / "work" / "sub"),
        )


def test_claim_symlink_alias_rejected(ctx, tmp_path):
    """测试 5：symlink 别名指向同一物理路径 → 拒绝。"""
    chain, repo = ctx
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real)
    repo.claim_root(
        binding_id="b1",
        agent_run_id=chain["agent_run_id"],
        attempt_id=chain["attempt_id"],
        root_path=str(real),
    )
    with pytest.raises(RuntimeConflictError, match=ROOT_CLAIM_CONFLICT):
        repo.claim_root(
            binding_id="b2",
            agent_run_id=chain["agent_run_id"],
            attempt_id=chain["attempt_id"],
            root_path=str(link),
        )


@pytest.mark.skipif(
    sys.platform != "darwin", reason="normcase 大小写归一仅对 macOS 生效"
)
def test_claim_case_alias_rejected_darwin(ctx, tmp_path):
    """测试 5（macOS）：大小写别名视为同根。"""
    chain, repo = ctx
    repo.claim_root(
        binding_id="b1",
        agent_run_id=chain["agent_run_id"],
        attempt_id=chain["attempt_id"],
        root_path=str(tmp_path / "Work"),
    )
    with pytest.raises(RuntimeConflictError, match=ROOT_CLAIM_CONFLICT):
        repo.claim_root(
            binding_id="b2",
            agent_run_id=chain["agent_run_id"],
            attempt_id=chain["attempt_id"],
            root_path=str(tmp_path / "work"),
        )


def test_claim_failed_migration_keeps_old_claims(ctx, tmp_path):
    """测试 6：活跃 binding 扩根失败（claims 冲突）→ 旧 claims 不转移不动摇。"""
    chain, repo = ctx
    repo.claim_root(
        binding_id="b1",
        agent_run_id=chain["agent_run_id"],
        attempt_id=chain["attempt_id"],
        root_path=str(tmp_path / "work"),
    )
    with pytest.raises(RuntimeConflictError, match=ROOT_CLAIM_CONFLICT):
        repo.claim_root(
            binding_id="b2",
            agent_run_id=chain["agent_run_id"],
            attempt_id=chain["attempt_id"],
            root_path=str(tmp_path / "work" / "extend"),
        )
    assert repo.claim_for_root(str(tmp_path / "work")) is not None
    assert repo.claim_for_root(str(tmp_path / "work" / "extend")) is None


def test_supersede_updates_run_epoch(ctx, tmp_path):
    """F.1：epoch 只随 binding 迁移变，且联动 agent_runs（fence 基准）。"""
    chain, repo = ctx
    binding = repo.create_binding(
        agent_run_id=chain["agent_run_id"],
        attempt_id=chain["attempt_id"],
        owner_id=OWNER,
        root_path=str(tmp_path / "work"),
    )
    new_epoch = repo.supersede_binding(binding_id=binding["binding_id"], expected_epoch=1)
    assert new_epoch == 2
    run = repo.get_agent_run(chain["agent_run_id"])
    assert run["workspace_epoch"] == 2
    assert repo.get_binding(binding["binding_id"])["status"] == "SUPERSEDED"


# ------------------------------------------------------------------ G1 补（B.3）
# opaque ID → 框架目录 ID 映射：ID 不直接成为物理路径权威（3.txt B.3）。
def test_directory_id_for_registers_then_returns_stable(repo):
    """G1：首次查未登记 → 登记并返回 directory_id；重复查幂等返回同一值。"""
    first = repo.directory_id_for("subagent-1780127110-469bfd0e", kind="run_id")
    second = repo.directory_id_for("subagent-1780127110-469bfd0e", kind="run_id")
    assert first == second == "subagent-1780127110-469bfd0e"
    with repo._runtime_connection() as conn:
        rows = conn.execute(
            "SELECT opaque_id, id_kind, directory_id FROM id_path_mapping"
        ).fetchall()
    assert len(rows) == 1
    assert rows[0]["directory_id"] == first
    assert rows[0]["id_kind"] == "run_id"


def test_register_id_path_idempotent(repo):
    """G1：重复登记幂等，不产生第二行、不改首次登记值。"""
    a = repo.register_id_path("subagent-abc123", kind="run_id")
    b = repo.register_id_path("subagent-abc123", kind="run_id")
    assert a == b
    with repo._runtime_connection() as conn:
        count = conn.execute(
            "SELECT COUNT(*) AS n FROM id_path_mapping WHERE opaque_id = ?",
            ("subagent-abc123",),
        ).fetchone()["n"]
    assert count == 1


def test_directory_id_matches_local_validation(repo):
    """G1：DB 登记值 == 模块级拒绝式校验结果（两路接线一致，无库环境不漂移）。"""
    opaque = "subagent-1780127110-deadbeef"
    assert repo.directory_id_for(opaque, kind="run_id") == directory_id_for_opaque(
        opaque, kind="run_id"
    )


def test_invalid_id_rejected_fail_closed(repo):
    """G1：非法 ID（路径形态）→ OpaqueIdError，不落表、不返回路径段。"""
    for bad in ("../etc/passwd", "/abs/path", "a b"):
        with pytest.raises(OpaqueIdError):
            repo.register_id_path(bad, kind="run_id")
        with pytest.raises(OpaqueIdError):
            repo.directory_id_for(bad, kind="run_id")
        with pytest.raises(OpaqueIdError):
            directory_id_for_opaque(bad, kind="run_id")
    with repo._runtime_connection() as conn:
        assert conn.execute("SELECT COUNT(*) AS n FROM id_path_mapping").fetchone()["n"] == 0


# ------------------------------------------------------------------ G2 补（H.8/3.txt:303）
# artifact record 主键独立 + 内容寻址 store：两个不同路径即使内容相同，
# 也必须各有一条记录（修复前 INSERT OR IGNORE + digest 主键坍缩）。
def test_publish_same_content_two_paths_keeps_two_records(ctx, tmp_path):
    """G2（3.txt:303）：同内容不同路径 → 两条记录，artifact_record_id 独立，
    content_digest 相同，content_path 指向同一内容寻址 store 文件（单副本）。"""
    chain, repo = ctx
    shared = tmp_path / "shared"
    staging = tmp_path / "staging"
    shared.mkdir()
    staging.mkdir()
    pub = _staged_publish(repo, chain, shared, staging, "a.txt", "same")
    (staging / "b.txt").write_text("same", encoding="utf-8")
    repo.stage_publish_manifest(
        pub["publish_id"],
        [
            {"path": "a.txt", "kind": "created", "postimage_digest": sha256_of(staging / "a.txt")},
            {"path": "b.txt", "kind": "created", "postimage_digest": sha256_of(staging / "b.txt")},
        ],
    )
    repo.publish(publish_id=pub["publish_id"], staging_root=staging, shared_root=shared)

    artifacts = repo.artifacts_for_attempt(chain["attempt_id"])
    assert sorted(a["rel_path"] for a in artifacts) == ["a.txt", "b.txt"]
    ids = {a["artifact_record_id"] for a in artifacts}
    digests = {a["content_digest"] for a in artifacts}
    paths = {a["content_path"] for a in artifacts}
    assert len(ids) == 2  # 主键独立：不坍缩
    assert len(digests) == 1  # 内容 hash 相同
    assert len(paths) == 1  # 内容寻址：store 单副本
    for a in artifacts:
        assert Path(a["content_path"]).read_text(encoding="utf-8") == "same"


def test_publish_store_immutable_and_shared_across_attempts(ctx, tmp_path):
    """G2：两次发布同内容（不同路径）→ 同一 store 文件（内容寻址去重），
    记录各自独立；store 内容保持发布时冻结（immutable）。"""
    chain, repo = ctx
    shared = tmp_path / "shared"
    staging = tmp_path / "staging"
    shared.mkdir()
    staging.mkdir()
    pub1 = _staged_publish(repo, chain, shared, staging, "a.txt", "stable")
    repo.publish(publish_id=pub1["publish_id"], staging_root=staging, shared_root=shared)
    pub2 = _staged_publish(repo, chain, shared, staging, "b.txt", "stable")
    repo.publish(publish_id=pub2["publish_id"], staging_root=staging, shared_root=shared)

    artifacts = repo.artifacts_for_attempt(chain["attempt_id"])
    assert len(artifacts) == 2
    assert len({a["artifact_record_id"] for a in artifacts}) == 2
    assert len({a["content_path"] for a in artifacts}) == 1  # 同 digest 单副本
    # store 内容保持发布时冻结（immutable：二次发布不改首次 store 文件）。
    assert Path(artifacts[0]["content_path"]).read_text(encoding="utf-8") == "stable"


def test_legacy_artifact_schema_migrates_pk_and_backfills_digest(tmp_path):
    """G2：存量库（旧 artifact_records：artifact_id 主键存 digest）打开即迁移：
    主键改名 artifact_record_id、补 content_digest 回填旧值、content_path 空
    （旧记录无 store 内容，load 回退 live 兼容）。"""
    import sqlite3

    db = tmp_path / "runtime.db"
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE artifact_records (artifact_id TEXT PRIMARY KEY, attempt_id TEXT NOT NULL, "
        "agent_run_id TEXT NOT NULL, publish_id TEXT NOT NULL DEFAULT '', rel_path TEXT NOT NULL, "
        "digest TEXT NOT NULL, size INTEGER NOT NULL DEFAULT 0, created_at REAL NOT NULL, "
        "UNIQUE(artifact_id))"
    )
    conn.execute(
        "INSERT INTO artifact_records(artifact_id, attempt_id, agent_run_id, publish_id, "
        "rel_path, digest, size, created_at) VALUES('abc123digest', 'att1', 'run1', 'pub1', "
        "'a.txt', 'abc123digest', 5, 1.0)"
    )
    conn.commit()
    conn.close()

    repo = RuntimeRepository(db)
    with repo._runtime_connection() as c:
        cols = [r["name"] for r in c.execute("PRAGMA table_info(artifact_records)").fetchall()]
        pk = [r["name"] for r in c.execute("PRAGMA table_info(artifact_records)").fetchall() if r["pk"]]
        row = c.execute(
            "SELECT artifact_record_id, content_digest, content_path FROM artifact_records"
        ).fetchone()
    assert pk == ["artifact_record_id"]  # 主键独立化
    assert "content_digest" in cols and "content_path" in cols
    assert row["artifact_record_id"] == "abc123digest"
    assert row["content_digest"] == "abc123digest"  # 旧值回填
    assert row["content_path"] == ""  # 无 store 内容 → load 回退 live
