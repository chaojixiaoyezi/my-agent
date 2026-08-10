"""R2 权威操作测试（3.txt §5 测试 4-13 + F/G/H 不变量）。

覆盖：三层 fence（F.6/F.7，测试 9）、ToolOperation 状态机（G.1-G.5）、
资源锁（G.6-G.9，测试 11/12）、mutation 账（G.12-G.14）、PublishOperation
（H.2-H.9，测试 10/13）、root claim 规范化（测试 5/6）、binding 复用
（D.6，测试 4）、epoch 联动（F.1）。
"""

from __future__ import annotations

import os
import platform
import sys
from pathlib import Path

import pytest

from agent_py_agent.agent.runtime_db.operations import (
    RESOURCE_VERSION_CONFLICT,
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
    assert artifacts[0]["digest"] == sha256_of(shared / "a.txt")


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
