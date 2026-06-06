"""LLM: regression tests for same-parent subagent domain dedupe.

函数/模块用途: 覆盖真实 E2E 中 root 重复创建 checkout/quality coordinator 的问题。
"""

from __future__ import annotations

import json
from pathlib import Path

from agent_py_agent.agent.subagents.manager import SubAgentManager
from agent_py_agent.agent.subagents.services.hierarchy.scheduler import (
    HierarchyChildSpec,
    HierarchyScheduleRequest,
)


def test_hierarchy_schedule_allows_duplicate_coordinator_domains(tmp_path):
    manager = SubAgentManager(tmp_path)
    root = manager.create_run(goal="shopping root", thought="orchestrate", plan=["plan"])

    first = manager.hierarchy.schedule_child_runs(
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
    duplicate = manager.hierarchy.schedule_child_runs(
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
    assert duplicate.blocked is False
    assert duplicate.reason == "created"
    assert not hasattr(duplicate, "scheduling_warnings")
    assert manager.load(root.id).child_ids == [*first.created_run_ids, *duplicate.created_run_ids]


def test_hierarchy_schedule_allows_generic_numbered_checker_siblings(tmp_path):
    manager = SubAgentManager(tmp_path)
    root = manager.create_run(goal="root", thought="orchestrate", plan=["split"])
    child = manager.create_run(
        goal="child one", thought="work", plan=["work"],
        parent_id=root.id, root_id=root.id, depth=1,
    )

    result = manager.hierarchy.schedule_child_runs(
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


def test_hierarchy_schedule_duplicate_domain_ignores_shared_filesystem_paths(tmp_path):
    manager = SubAgentManager(tmp_path)
    root = manager.create_run(goal="shopping root", thought="orchestrate", plan=["plan"])
    child = manager.create_run(
        goal="coordinate shopping site",
        thought="coordinate",
        plan=["split"],
        parent_id=root.id,
        root_id=root.id,
        depth=1,
    )

    result = manager.hierarchy.schedule_child_runs(
        params=HierarchyScheduleRequest(
            parent_run_id=child.id,
            child_specs=[
                HierarchyChildSpec(
                    goal=(
                        "在 /Users/example/my-claude-code/deliverables/stage7_shop_complete/build "
                        "交付静态示例网站 HTML/CSS/JS。"
                    ),
                    role="coordinator",
                    agent_name="小小傻妞-前端Worker",
                ),
                HierarchyChildSpec(
                    goal=(
                        "协调测试子代理，为示例网站 demo 创建验收测试。测试文件写到 "
                        "/Users/example/my-claude-code/deliverables/stage7_shop_complete/build/tests/。"
                    ),
                    role="coordinator",
                    agent_name="小小傻妞-测试协调",
                ),
            ],
            apply=True,
        )
    )

    assert result.blocked is False
    assert len(result.created_run_ids) == 2


def test_hierarchy_schedule_duplicate_domain_ignores_depth_markers(tmp_path):
    manager = SubAgentManager(tmp_path)
    root = manager.create_run(goal="shopping root", thought="orchestrate", plan=["plan"])
    child = manager.create_run(
        goal="coordinate shopping site",
        thought="coordinate",
        plan=["split"],
        parent_id=root.id,
        root_id=root.id,
        depth=1,
    )

    result = manager.hierarchy.schedule_child_runs(
        params=HierarchyScheduleRequest(
            parent_run_id=child.id,
            child_specs=[
                HierarchyChildSpec(
                    goal="创建 depth=3 孙孙节点完成 index.html/register.html/style.css/app.js。",
                    role="child_coordinator",
                    agent_name="小小傻妞-前端协调A",
                ),
                HierarchyChildSpec(
                    goal="创建 depth=3 孙孙节点完成 login.html/flow-a.html/flow-b.html。",
                    role="child_coordinator",
                    agent_name="小小傻妞-前端协调B",
                ),
            ],
            apply=True,
        )
    )

    assert result.blocked is False
    assert len(result.created_run_ids) == 2


def test_hierarchy_schedule_allows_duplicate_verified_leaf_targets(tmp_path):
    manager = SubAgentManager(tmp_path)
    parent = _auth_parent_with_verified_leaf(manager)

    duplicate = manager.hierarchy.schedule_child_runs(
        params=HierarchyScheduleRequest(
            parent_run_id=parent.id,
            child_specs=[
                HierarchyChildSpec(
                    goal="rewrite register.html and login.html",
                    role="leaf_worker",
                    agent_name="auth-leaf-writer",
                    attributes={"output_refs": ["deliverables/shop/build/register.html", "deliverables/shop/build/login.html"]},
                )
            ],
            apply=True,
        )
    )
    sibling = manager.hierarchy.schedule_child_runs(
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

    assert duplicate.blocked is False
    assert duplicate.reason == "created"
    assert not hasattr(duplicate, "scheduling_warnings")
    assert len(duplicate.created_run_ids) == 1
    assert sibling.blocked is False
    assert len(sibling.created_run_ids) == 1


def test_hierarchy_schedule_allows_explicit_repair_leaf_for_existing_target(tmp_path):
    manager = SubAgentManager(tmp_path)
    parent = _shared_parent_with_verified_leaf(manager)

    repair = manager.hierarchy.schedule_child_runs(
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


def test_hierarchy_schedule_allows_active_duplicate_repair_child(tmp_path):
    manager = SubAgentManager(tmp_path)
    parent = _shared_parent_with_verified_leaf(manager)
    first = _schedule_repair_worker(manager, parent.id, "根据失败 QA refs 修复 app.js 按钮绑定。", "qa-repair-worker")
    duplicate = _schedule_repair_worker(manager, parent.id, "修复失败 QA ref 指出的按钮绑定和 retry 逻辑。", "qa-repair-worker")

    assert len(first.created_run_ids) == 1
    assert duplicate.blocked is False
    assert len(duplicate.created_run_ids) == 1

    renamed_duplicate = _schedule_repair_worker(
        manager,
        parent.id,
        "最终修复 case01 失败 QA 指出的按钮绑定。",
        "case01-final-repair-worker",
    )

    assert renamed_duplicate.blocked is False
    assert len(renamed_duplicate.created_run_ids) == 1


def _schedule_repair_worker(manager: SubAgentManager, parent_id: str, goal: str, agent_name: str):
    return manager.hierarchy.schedule_child_runs(
        params=HierarchyScheduleRequest(
            parent_run_id=parent_id,
            child_specs=[HierarchyChildSpec(goal=goal, role="worker", agent_name=agent_name)],
            apply=True,
        )
    )


def test_hierarchy_schedule_allows_leaf_referencing_shared_assets(tmp_path):
    manager = SubAgentManager(tmp_path)
    parent = _shared_parent_with_verified_leaf(manager)

    cart = manager.hierarchy.schedule_child_runs(
        params=HierarchyScheduleRequest(
            parent_run_id=parent.id,
            child_specs=[
                HierarchyChildSpec(
                    goal=(
                        "写 flow-a.html 和 flow-b.html\n"
                        "## 目标\n"
                        "- /tmp/site/build/flow-a.html\n"
                        "- /tmp/site/build/flow-b.html\n"
                        "## 内容\n"
                        "- flow-a.html 引入 style.css 和 app.js\n"
                        "- flow-b.html 链接到 flow-done.html"
                    ),
                    role="leaf_worker",
                    agent_name="小小小傻妞-cart-writer",
                )
            ],
            apply=True,
        )
    )

    assert cart.blocked is False
    assert len(cart.created_run_ids) == 1


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


def _mark_leaf_verified_with_artifacts(manager: SubAgentManager, leaf, artifact_paths: list[str]) -> None:
    Path(leaf.output_json).write_text(
        json.dumps({"artifacts": [{"path": item} for item in artifact_paths]}),
        encoding="utf-8",
    )
    leaf.status = "DONE"
    leaf.verification_status = "VERIFIED"
    manager.save(leaf)
