"""统一授权查询门（3.txt B.4/B.5）离线测试。

用真实 SubAgentManager + create_run 造一棵树：
    main（主代理域，无 run 记录）
    ├── run-a（owner=local/main）
    │   └── run-a1（run-a 的子代）
    └── run-b（run-a 的兄弟，owner=local/main）
树外：run-x（owner=other/owner，另一 owner 域）
"""

from __future__ import annotations

import pytest

from agent_py_agent.agent.subagents.authorization_gate import (
    AuthorizationError,
    OperationRequest,
    authorize_operation,
)
from agent_py_agent.agent.subagents.manager import SubAgentManager


@pytest.fixture
def tree(tmp_path):
    manager = SubAgentManager(tmp_path / "workspace", owner_id="local/main")
    run_a = manager.create_run(goal="a", root_id="run-main", parent_id="run-main")
    run_a1 = manager.create_run(goal="a1", root_id="run-main", parent_id=run_a.id)
    run_b = manager.create_run(goal="b", root_id="run-main", parent_id="run-main")
    run_x = manager.create_run(goal="x", root_id="run-x", parent_id="run-x", owner="other/owner")
    orphan = manager.create_run(goal="orphan", root_id="run-main", parent_id="run-ghost")
    return SimpleTree(manager, run_a, run_a1, run_b, run_x, orphan)


class SimpleTree:
    def __init__(self, manager, run_a, run_a1, run_b, run_x, orphan):
        self.manager = manager
        self.run_a = run_a
        self.run_a1 = run_a1
        self.run_b = run_b
        self.run_x = run_x
        self.orphan = orphan


def _req(operation, run_id, requester_run_id="", requester_owner="local/main"):
    return OperationRequest(
        operation=operation,
        run_id=run_id,
        requester_owner=requester_owner,
        requester_run_id=requester_run_id,
    )


def test_unknown_operation_rejected(tree):
    with pytest.raises(AuthorizationError, match="未知操作"):
        authorize_operation(tree.manager, _req("explode", tree.run_a.id))


def test_path_injection_rejected(tree):
    with pytest.raises(AuthorizationError, match="非法 run_id"):
        authorize_operation(tree.manager, _req("cancel", "../../etc/passwd"))


def test_missing_run_rejected(tree):
    # 记录不存在透传 FileNotFoundError（AuthorizationError 只表示"存在但无权"；
    # 调用方靠 FileNotFoundError 走名册自纠，见 capability.py）。
    with pytest.raises(FileNotFoundError, match="不存在"):
        authorize_operation(tree.manager, _req("cancel", "run-0000-00000000"))


def test_owner_mismatch_rejected(tree):
    with pytest.raises(AuthorizationError, match="owner"):
        authorize_operation(tree.manager, _req("cancel", tree.run_x.id))


def test_owner_match_allowed(tree):
    task = authorize_operation(tree.manager, _req("cancel", tree.run_a.id))
    assert task.id == tree.run_a.id


def test_requester_without_owner_skips_owner_check(tree):
    # 无 owner 权威信息（旧记录/无配置）→ 跳过，不误杀。
    task = authorize_operation(
        tree.manager,
        OperationRequest(operation="cancel", run_id=tree.run_a.id),
    )
    assert task.id == tree.run_a.id


def test_requester_operates_own_run(tree):
    task = authorize_operation(
        tree.manager, _req("cancel", tree.run_a.id, requester_run_id=tree.run_a.id)
    )
    assert task.id == tree.run_a.id


def test_requester_operates_direct_child(tree):
    task = authorize_operation(
        tree.manager, _req("cancel", tree.run_a1.id, requester_run_id=tree.run_a.id)
    )
    assert task.id == tree.run_a1.id


def test_requester_operates_grandchild_via_chain(tree):
    task = authorize_operation(
        tree.manager, _req("cancel", tree.run_a1.id, requester_run_id=tree.run_a.id)
    )
    assert task.id == tree.run_a1.id


def test_requester_operates_sibling_rejected(tree):
    with pytest.raises(AuthorizationError, match="子树"):
        authorize_operation(
            tree.manager, _req("cancel", tree.run_b.id, requester_run_id=tree.run_a.id)
        )


def test_requester_operates_parent_rejected(tree):
    # 子代理不能操作自己的父任务：父是主代理域（无 subagent 记录）→
    # load 抛 FileNotFoundError 透传（AuthorizationError 只表示"存在但无权"）。
    with pytest.raises(FileNotFoundError, match="不存在"):
        authorize_operation(
            tree.manager, _req("cancel", "run-main", requester_run_id=tree.run_a.id)
        )


def test_orphan_chain_broken_rejected_for_subagent(tree):
    # 子代理操作孤儿（父记录被删）：链断且不是树根 → fail-closed 拒绝。
    with pytest.raises(AuthorizationError, match="子树"):
        authorize_operation(
            tree.manager, _req("cancel", tree.orphan.id, requester_run_id=tree.run_a.id)
        )


def test_tree_root_operates_orphan_allowed(tree):
    # 树根（主代理域，requester=root_id，无 subagent 记录）操作孤儿 → 放行。
    task = authorize_operation(
        tree.manager, _req("cancel", tree.orphan.id, requester_run_id="run-main")
    )
    assert task.id == tree.orphan.id


def test_all_registered_operations_pass_gate(tree):
    for operation in (
        "cancel",
        "dispatch",
        "resume",
        "takeover",
        "resolve_capability",
        "send_guidance",
    ):
        task = authorize_operation(tree.manager, _req(operation, tree.run_a.id))
        assert task.id == tree.run_a.id
