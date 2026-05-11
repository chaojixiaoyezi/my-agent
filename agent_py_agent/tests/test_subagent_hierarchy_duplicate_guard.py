"""LLM: regression tests for same-parent subagent domain dedupe.

函数/模块用途: 覆盖真实 E2E 中 root 重复创建 checkout/quality coordinator 的问题。
"""

from __future__ import annotations

import json
from pathlib import Path

from agent_py_agent.agent.subagents.manager import SubAgentManager
from agent_py_agent.agent.subagents.services.hierarchy_scheduler import (
    HierarchyChildSpec,
    HierarchyScheduleRequest,
)


# LLM: test_hierarchy_schedule_blocks_duplicate_coordinator_domains covers real shopping E2E double-dispatch.
# 函数用途: 同一个父节点已有 checkout/quality coordinator 后，再创建同域 coordinator 必须被阻断。
def test_hierarchy_schedule_blocks_duplicate_coordinator_domains(tmp_path):
    manager = SubAgentManager(tmp_path)
    root = manager.create_run(goal="shopping root", thought="orchestrate", plan=["plan"])

    first = manager.schedule_child_runs(
        params=HierarchyScheduleRequest(
            parent_run_id=root.id,
            child_specs=[
                HierarchyChildSpec(
                    goal="build cart and checkout pages",
                    role="coordinator",
                    agent_name="cart-checkout-coordinator",
                ),
                HierarchyChildSpec(
                    goal="verify shopping pages",
                    role="coordinator",
                    agent_name="quality-coordinator",
                ),
            ],
            apply=True,
        )
    )
    duplicate = manager.schedule_child_runs(
        params=HierarchyScheduleRequest(
            parent_run_id=root.id,
            child_specs=[
                HierarchyChildSpec(
                    goal="build checkout page",
                    role="coordinator",
                    agent_name="checkout-coordinator",
                ),
                HierarchyChildSpec(
                    goal="verify all links",
                    role="checker",
                    agent_name="quality-checker",
                ),
            ],
            apply=True,
        )
    )

    assert len(first.created_run_ids) == 2
    assert duplicate.blocked is True
    assert duplicate.reason == "duplicate_child_domain:checkout"
    assert manager.load(root.id).child_ids == first.created_run_ids


# LLM: test_hierarchy_schedule_allows_generic_numbered_checker_siblings protects recovery trees.
# 函数用途: `grand-1/grand-2` 这类泛化编号 checker 不是同业务域重复，不能被同域去重误挡。
def test_hierarchy_schedule_allows_generic_numbered_checker_siblings(tmp_path):
    manager = SubAgentManager(tmp_path)
    root = manager.create_run(goal="root", thought="orchestrate", plan=["split"])
    child = manager.create_run(
        goal="child one", thought="work", plan=["work"],
        parent_id=root.id, root_id=root.id, depth=1,
    )

    result = manager.schedule_child_runs(
        params=HierarchyScheduleRequest(
            parent_run_id=child.id,
            child_specs=[
                HierarchyChildSpec(goal="grand one", role="checker", agent_name="grand-1"),
                HierarchyChildSpec(goal="grand two", role="checker", agent_name="grand-2"),
            ],
            apply=True,
        )
    )

    assert result.blocked is False
    assert len(result.created_run_ids) == 2


# LLM: test_hierarchy_schedule_blocks_duplicate_verified_leaf_targets covers R11 duplicate auth leaf creation.
# 函数用途: 同父级已有 DONE/VERIFIED leaf 写出 register/login 后，不能再派同一文件的 leaf。
def test_hierarchy_schedule_blocks_duplicate_verified_leaf_targets(tmp_path):
    manager = SubAgentManager(tmp_path)
    parent = _auth_parent_with_verified_leaf(manager)

    duplicate = manager.schedule_child_runs(
        params=HierarchyScheduleRequest(
            parent_run_id=parent.id,
            child_specs=[
                HierarchyChildSpec(
                    goal="rewrite register.html and login.html",
                    role="leaf_worker",
                    agent_name="auth-leaf-writer",
                )
            ],
            apply=True,
        )
    )
    sibling = manager.schedule_child_runs(
        params=HierarchyScheduleRequest(
            parent_run_id=parent.id,
            child_specs=[
                HierarchyChildSpec(
                    goal="write password-reset.html",
                    role="leaf_worker",
                    agent_name="password-reset-leaf",
                )
            ],
            apply=True,
        )
    )

    assert duplicate.blocked is True
    assert duplicate.reason == "duplicate_leaf_target:login.html"
    assert sibling.blocked is False
    assert len(sibling.created_run_ids) == 1


# LLM: test_hierarchy_schedule_allows_explicit_repair_leaf_for_existing_target locks R19 repair recovery.
# 函数用途: 已有 leaf 写过 app.js 后，明确“修复/补齐”任务仍可创建新的修复 leaf，避免 coordinator 卡死。
def test_hierarchy_schedule_allows_explicit_repair_leaf_for_existing_target(tmp_path):
    manager = SubAgentManager(tmp_path)
    parent = _shared_parent_with_verified_leaf(manager)

    repair = manager.schedule_child_runs(
        params=HierarchyScheduleRequest(
            parent_run_id=parent.id,
            child_specs=[
                HierarchyChildSpec(
                    goal="修复 app.js，补齐 getUrlParam 和 setUrlParam 函数。",
                    role="leaf_worker",
                    agent_name="app-js-repair-worker",
                )
            ],
            apply=True,
        )
    )

    assert repair.blocked is False
    assert len(repair.created_run_ids) == 1


# LLM: _auth_parent_with_verified_leaf creates a parent with one completed auth leaf fixture.
# 函数用途: 构造 leaf 目标去重测试用的父节点和已验证子节点，避免测试主体过长。
def _auth_parent_with_verified_leaf(manager: SubAgentManager):
    root = manager.create_run(goal="shopping root", thought="orchestrate", plan=["plan"])
    parent = manager.create_run(
        goal="auth coordinator",
        thought="coordinate auth",
        plan=["split auth"],
        parent_id=root.id,
        root_id=root.id,
        depth=1,
        role="coordinator",
        agent_name="auth-coordinator",
    )
    done_leaf = manager.create_run(
        goal="write register.html and login.html",
        thought="write auth pages",
        plan=["write pages"],
        parent_id=parent.id,
        root_id=root.id,
        depth=2,
        role="leaf_worker",
        agent_name="auth-worker",
    )
    _mark_leaf_verified_with_artifacts(
        manager,
        done_leaf,
        ["deliverables/shop/build/register.html", "deliverables/shop/build/login.html"],
    )
    return parent


# LLM: _shared_parent_with_verified_leaf mirrors the R19 shared-assets coordinator fixture.
# 函数用途: 构造 app.js 已由同父级 leaf 完成的场景，用来验证后续修复 leaf 不被误拦。
def _shared_parent_with_verified_leaf(manager: SubAgentManager):
    root = manager.create_run(goal="shopping root", thought="orchestrate", plan=["plan"])
    parent = manager.create_run(
        goal="shared assets coordinator",
        thought="coordinate shared js/css",
        plan=["split shared assets"],
        parent_id=root.id,
        root_id=root.id,
        depth=1,
        role="coordinator",
        agent_name="shared-assets-coordinator",
    )
    done_leaf = manager.create_run(
        goal="write app.js",
        thought="write shared app logic",
        plan=["write app.js"],
        parent_id=parent.id,
        root_id=root.id,
        depth=2,
        role="leaf_worker",
        agent_name="app-js-worker",
    )
    _mark_leaf_verified_with_artifacts(
        manager,
        done_leaf,
        ["deliverables/shop/build/app.js"],
    )
    return parent


# LLM: _mark_leaf_verified_with_artifacts writes only structured artifact refs for dedupe tests.
# 函数用途: 把 leaf fixture 标记为 DONE/VERIFIED，并在 output.json 里写 artifact 路径引用。
def _mark_leaf_verified_with_artifacts(manager: SubAgentManager, leaf, artifact_paths: list[str]) -> None:
    Path(leaf.output_json).write_text(
        json.dumps({"artifacts": [{"path": item} for item in artifact_paths]}),
        encoding="utf-8",
    )
    leaf.status = "DONE"
    leaf.verification_status = "VERIFIED"
    manager.save(leaf)
