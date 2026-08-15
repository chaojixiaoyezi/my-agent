"""B.5 授权门 DB 权威升级测试（R1-4 + F8 fail-closed）。

带 owner_home_dir 的 manager 下，门必须同时验证 owner/TaskRun/
parent+delegation/current attempt/WorkspaceBinding（B.5），任一环缺失
或失配 → fail-closed。F8 起：有 home 时权威库不可用或目标无权威记录
同样 fail-closed（B.6 以 DB 为权威）；仅无 home 上下文维持文件层防线。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agent_py_agent.agent.runtime_db.repository import RuntimeRepository
from agent_py_agent.agent.subagents.authorization_gate import (
    AuthorizationError,
    OperationRequest,
    authorize_operation,
    authorize_tree_scope,
)
from agent_py_agent.agent.subagents.manager import SubAgentManager

OWNER = "local/main"


@pytest.fixture
def ctx(tmp_path):
    manager = SubAgentManager(
        tmp_path / "workspace",
        owner_id=OWNER,
        owner_home_dir=str(tmp_path / "home"),
    )
    repo = RuntimeRepository(Path(manager.owner_home_dir) / "runtime.db")
    root = manager.create_run(goal="root", root_id="run-main", parent_id="run-main")
    child = manager.create_run(goal="child", root_id="run-main", parent_id=root.id)
    return _Ctx(manager, repo, root, child)


class _Ctx:
    def __init__(self, manager, repo, root, child):
        self.manager = manager
        self.repo = repo
        self.root = root
        self.child = child


def _req(operation, run_id, requester_owner=OWNER):
    return OperationRequest(operation=operation, run_id=run_id, requester_owner=requester_owner)


def test_authority_chain_clean_allows(ctx):
    task = authorize_operation(ctx.manager, _req("cancel", ctx.root.id))
    assert task.id == ctx.root.id
    task = authorize_operation(ctx.manager, _req("inspect", ctx.child.id))
    assert task.id == ctx.child.id


def test_db_owner_mismatch_rejected_even_if_file_says_ok(ctx):
    # task.json owner 可被篡改；tasks.owner_id 权威，与请求方不符 → 拒绝。
    with ctx.repo._runtime_connection() as conn:
        conn.execute(
            "UPDATE tasks SET owner_id = 'other/owner' "
            "WHERE task_id = (SELECT task_id FROM task_runs WHERE task_run_id = "
            "(SELECT task_run_id FROM agent_runs WHERE run_id = ?))",
            (ctx.root.id,),
        )
        conn.commit()
    with pytest.raises(AuthorizationError, match="DB 权威 owner"):
        authorize_operation(ctx.manager, _req("cancel", ctx.root.id))


def test_missing_task_run_rejected(ctx):
    with ctx.repo._runtime_connection() as conn:
        conn.execute(
            "DELETE FROM task_runs WHERE task_run_id = "
            "(SELECT task_run_id FROM agent_runs WHERE run_id = ?)",
            (ctx.root.id,),
        )
        conn.commit()
    with pytest.raises(AuthorizationError, match="权威 TaskRun 缺失"):
        authorize_operation(ctx.manager, _req("cancel", ctx.root.id))


def test_missing_current_attempt_rejected(ctx):
    with ctx.repo._runtime_connection() as conn:
        conn.execute(
            "DELETE FROM agent_attempts WHERE attempt_id = "
            "(SELECT current_attempt_id FROM agent_runs WHERE run_id = ?)",
            (ctx.root.id,),
        )
        conn.execute(
            "UPDATE agent_runs SET current_attempt_id = '' WHERE run_id = ?",
            (ctx.root.id,),
        )
        conn.commit()
    with pytest.raises(AuthorizationError, match="权威 current attempt 缺失"):
        authorize_operation(ctx.manager, _req("cancel", ctx.root.id))


def test_missing_delegation_rejected_for_child(ctx):
    with ctx.repo._runtime_connection() as conn:
        conn.execute(
            "DELETE FROM delegations WHERE child_agent_run_id = "
            "(SELECT agent_run_id FROM agent_runs WHERE run_id = ?)",
            (ctx.child.id,),
        )
        conn.execute(
            "UPDATE agent_runs SET delegation_id = '' WHERE run_id = ?",
            (ctx.child.id,),
        )
        conn.commit()
    with pytest.raises(AuthorizationError, match="权威 delegation 缺失/失配"):
        authorize_operation(ctx.manager, _req("inspect", ctx.child.id))


def test_superseded_binding_rejected(ctx):
    # 曾建 binding 后被迁移（SUPERSEDED）→ 旧 run fail-closed（D.9）。
    agent_run = ctx.repo.agent_run_for_run_id(ctx.root.id)
    attempt = ctx.repo.current_attempt(agent_run["agent_run_id"])
    binding = ctx.repo.create_binding(
        agent_run_id=agent_run["agent_run_id"],
        attempt_id=attempt["attempt_id"],
        owner_id=OWNER,
        root_path="/home/u/work",
    )
    ctx.repo.supersede_binding(binding_id=binding["binding_id"], expected_epoch=1)
    with pytest.raises(AuthorizationError, match="WorkspaceBinding 非 ACTIVE"):
        authorize_operation(ctx.manager, _req("cancel", ctx.root.id))


def test_never_bound_run_allowed(ctx):
    # 未执行（未建 binding）→ binding 校验放行。
    task = authorize_operation(ctx.manager, _req("cancel", ctx.root.id))
    assert task.id == ctx.root.id


def test_run_without_authority_record_fails_closed(tmp_path):
    """F8：home 在但目标无权威记录（旁路/幻影/未迁移存量）→ B.6 以 DB 为准拒绝。"""
    workspace = tmp_path / "workspace"
    legacy = SubAgentManager(workspace, owner_id=OWNER)  # 无 home → 只写文件层
    run = legacy.create_run(goal="存量", root_id="run-main", parent_id="run-main")
    modern = SubAgentManager(
        workspace, owner_id=OWNER, owner_home_dir=str(tmp_path / "home")
    )
    with pytest.raises(AuthorizationError, match="权威记录缺失"):
        authorize_operation(modern, _req("inspect", run.id))


def test_missing_repo_with_home_fails_closed(ctx):
    """F8：attach 静默降级（home 在但 runtime_db=None）→ 门拒绝，不跳过 B.5。"""
    ctx.manager.runtime_db = None
    with pytest.raises(AuthorizationError, match="权威库不可用"):
        authorize_operation(ctx.manager, _req("cancel", ctx.root.id))


def test_tree_scope_validates_requester_authority(ctx):
    # 请求方自身 run 权威损坏 → 树级扫描拒绝。
    with ctx.repo._runtime_connection() as conn:
        conn.execute(
            "UPDATE agent_runs SET current_attempt_id = '' WHERE run_id = ?",
            (ctx.child.id,),
        )
        conn.commit()
    with pytest.raises(AuthorizationError, match="权威 current attempt 缺失"):
        authorize_tree_scope(
            ctx.manager,
            OperationRequest(operation="inspect", run_id=ctx.child.id, requester_run_id=ctx.child.id),
            "run-main",
        )
