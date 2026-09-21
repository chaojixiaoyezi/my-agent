"""执行换代保留已确认资源，未完成写入仍须阻断后续使用。"""

import pytest

from agent_py_agent.agent.runtime_db.repository import (
    RuntimeConflictError,
    RuntimeRepository,
)


# LLM: 仅为临时测试库构造三种既有资源事实；不调用工具或修改真实任务状态。
# 函数用途: 准备同一旧执行轮的已确认、未完成和原本不确定的资源，供换代回归使用。
def _seed_mutations(repo, attempt_id):
    repo.begin_mutation(canonical_scope="confirmed", attempt_id=attempt_id)
    stable = dict(repo.mark_mutation_stable("confirmed"))
    repo.begin_mutation(canonical_scope="unfinished", attempt_id=attempt_id)
    dirty = dict(repo.mark_mutation_dirty(
        canonical_scope="already-dirty", reason="prior-uncertain-effect",
        attempt_id=attempt_id,
    ))
    return stable, dirty


# LLM: 验证下一轮能复用已确认资源，同时未完成和旧 DIRTY 仍拒绝写入；不只断言 SQL 文本。
# 函数用途: 检查换代后的可用性、原证据不变及不确定资源的保守边界。
def _assert_resource_outcomes(repo, new_attempt_id, stable, dirty, reason):
    assert dict(repo.mutation_for_scope("confirmed")) == stable
    assert dict(repo.mutation_for_scope("already-dirty")) == dirty
    unfinished = repo.mutation_for_scope("unfinished")
    assert unfinished["state"] == "DIRTY"
    assert unfinished["dirty_reason"] == reason
    renewed = repo.begin_mutation(canonical_scope="confirmed", attempt_id=new_attempt_id)
    assert renewed["attempt_id"] == new_attempt_id
    assert renewed["state"] == "MUTATING"
    for scope in ("unfinished", "already-dirty"):
        with pytest.raises(RuntimeConflictError, match="非 STABLE"):
            repo.begin_mutation(canonical_scope=scope, attempt_id=new_attempt_id)


@pytest.mark.parametrize("settle_previous", [True, False], ids=["normal-continuation", "active-takeover"])
def test_next_generation_preserves_confirmed_resources(tmp_path, settle_previous):
    repo = RuntimeRepository(tmp_path / "runtime.db")
    chain = repo.record_run_creation(owner_id="local/main", run_id="main", goal="核对")
    stable, dirty = _seed_mutations(repo, chain["attempt_id"])
    if settle_previous:
        result = repo.settle_agent_attempt(
            agent_run_id=chain["agent_run_id"], attempt_id=chain["attempt_id"],
        )
        assert result["settled"]

    new = repo.create_attempt(chain["agent_run_id"])

    assert repo.get_attempt(chain["attempt_id"])["status"] == (
        "done" if settle_previous else "cancelled"
    )
    repo.verify_fence(agent_run_id=chain["agent_run_id"], attempt_id=new["attempt_id"])
    with pytest.raises(RuntimeConflictError, match="已不是 current pointer"):
        repo.verify_fence(agent_run_id=chain["agent_run_id"], attempt_id=chain["attempt_id"])
    _assert_resource_outcomes(repo, new["attempt_id"], stable, dirty, "takeover_recovery")


def test_upgrade_reconciliation_preserves_confirmed_resources(tmp_path):
    repo = RuntimeRepository(tmp_path / "runtime.db")
    chain = repo.record_run_creation(owner_id="local/main", run_id="main", goal="核对")
    current = repo.create_attempt(chain["agent_run_id"])
    # 只在夹具中还原升级前留下的非 current 运行轮及其资源账，不授权真实旧轮继续执行。
    with repo.transaction() as conn:
        conn.execute(
            "UPDATE agent_attempts SET status='running', ended_at=0 WHERE attempt_id=?",
            (chain["attempt_id"],),
        )
    stable, dirty = _seed_mutations(repo, chain["attempt_id"])

    assert repo.reconcile_superseded_attempts() == [chain["attempt_id"]]

    _assert_resource_outcomes(
        repo, current["attempt_id"], stable, dirty, "stale_noncurrent_reconcile",
    )
    assert repo.reconcile_superseded_attempts() == []
