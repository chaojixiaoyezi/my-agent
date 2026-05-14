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


# LLM: duplicate coordinator domains are audit warnings, not hard orchestration blockers.
# 函数用途: 同一个父节点已有 checkout/quality coordinator 后，再创建同域 coordinator 只提示风险，不阻断 QA/修复协作。
def test_hierarchy_schedule_warns_duplicate_coordinator_domains(tmp_path):
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
    assert duplicate.blocked is False
    assert duplicate.reason == "created"
    assert duplicate.scheduling_warnings == ["duplicate_child_domain:checkout", "duplicate_child_domain:quality"]
    assert manager.load(root.id).child_ids == [*first.created_run_ids, *duplicate.created_run_ids]


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


# LLM: R53 showed duplicate-domain guard must ignore shared workspace paths in child goals.
# 函数用途: 两个不同 coordinator 都提到 `/Users/.../my-claude-code/...` 时，不能把路径里的 claude 当成重复领域。
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

    result = manager.schedule_child_runs(
        params=HierarchyScheduleRequest(
            parent_run_id=child.id,
            child_specs=[
                HierarchyChildSpec(
                    goal=(
                        "在 /Users/xiaoyezi/my-claude-code/deliverables/stage7_shop_complete/build "
                        "交付静态购物网站 HTML/CSS/JS。"
                    ),
                    role="coordinator",
                    agent_name="小小傻妞-前端Worker",
                ),
                HierarchyChildSpec(
                    goal=(
                        "协调测试子代理，为购物网站 demo 创建验收测试。测试文件写到 "
                        "/Users/xiaoyezi/my-claude-code/deliverables/stage7_shop_complete/build/tests/。"
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


# LLM: R56 showed Chinese coordinator names can fall back to goal text containing only generic depth words.
# 函数用途: 两个中文 coordinator 的 goal 都写 depth=3 时，不能把 depth 当成重复业务域。
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

    result = manager.schedule_child_runs(
        params=HierarchyScheduleRequest(
            parent_run_id=child.id,
            child_specs=[
                HierarchyChildSpec(
                    goal="创建 depth=3 孙孙节点完成 index.html/register.html/style.css/app.js。",
                    role="child_coordinator",
                    agent_name="小小傻妞-前端协调A",
                ),
                HierarchyChildSpec(
                    goal="创建 depth=3 孙孙节点完成 login.html/cart.html/checkout.html。",
                    role="child_coordinator",
                    agent_name="小小傻妞-前端协调B",
                ),
            ],
            apply=True,
        )
    )

    assert result.blocked is False
    assert len(result.created_run_ids) == 2


# LLM: test_hierarchy_schedule_warns_duplicate_verified_leaf_targets covers R73 audit-over-blocking.
# 函数用途: 同父级已有 DONE/VERIFIED leaf 写过同一文件时，调度应创建新任务并给父级审计提示，而不是硬阻断修复/协作。
def test_hierarchy_schedule_warns_duplicate_verified_leaf_targets(tmp_path):
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

    assert duplicate.blocked is False
    assert duplicate.reason == "created"
    assert duplicate.scheduling_warnings == ["duplicate_leaf_target:login.html"]
    assert len(duplicate.created_run_ids) == 1
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


# LLM: Active QA/repair duplicates should reuse or recover the current run instead of growing the tree.
# 函数用途: 复现真实恢复 E2E 中 root 反复创建 qa-repair-worker 的问题，要求调度层阻断无限扩容。
def test_hierarchy_schedule_blocks_active_duplicate_repair_child(tmp_path):
    manager = SubAgentManager(tmp_path)
    parent = _shared_parent_with_verified_leaf(manager)
    first = _schedule_repair_worker(manager, parent.id, "根据失败 QA refs 修复 app.js 按钮绑定。", "qa-repair-worker")
    duplicate = _schedule_repair_worker(manager, parent.id, "修复失败 QA ref 指出的按钮绑定和 retry 逻辑。", "qa-repair-worker")

    assert len(first.created_run_ids) == 1
    _assert_duplicate_repair_blocked(manager, parent.id, duplicate, first.created_run_ids[0])

    renamed_duplicate = _schedule_repair_worker(
        manager,
        parent.id,
        "最终修复 case01 失败 QA 指出的按钮绑定。",
        "case01-final-repair-worker",
    )

    _assert_duplicate_repair_blocked(manager, parent.id, renamed_duplicate, first.created_run_ids[0])


# LLM: _schedule_repair_worker keeps duplicate-repair tests focused on guard semantics.
# 函数用途: 创建一个同父级 repair worker 调度请求，复用 goal/name 参数。
def _schedule_repair_worker(manager: SubAgentManager, parent_id: str, goal: str, agent_name: str):
    return manager.schedule_child_runs(
        params=HierarchyScheduleRequest(
            parent_run_id=parent_id,
            child_specs=[HierarchyChildSpec(goal=goal, role="worker", agent_name=agent_name)],
            apply=True,
        )
    )


# LLM: _assert_duplicate_repair_blocked verifies active repair dedupe without repeating assertions.
# 函数用途: 断言重复 repair worker 被阻断，且父节点 child_ids 没有继续膨胀。
def _assert_duplicate_repair_blocked(manager: SubAgentManager, parent_id: str, result, first_run_id: str) -> None:
    assert result.blocked is True
    assert result.created_run_ids == []
    assert f"active_duplicate_child:{first_run_id}" in result.reason
    assert len(manager.load(parent_id).child_ids) == 2


# LLM: Referencing shared assets must not make a page worker claim ownership of those assets.
# 函数用途: 复现 R38 cart-writer 只“引入 app.js”却被当成 app.js 产物重复的真实 E2E 问题。
def test_hierarchy_schedule_allows_leaf_referencing_shared_assets(tmp_path):
    manager = SubAgentManager(tmp_path)
    parent = _shared_parent_with_verified_leaf(manager)

    cart = manager.schedule_child_runs(
        params=HierarchyScheduleRequest(
            parent_run_id=parent.id,
            child_specs=[
                HierarchyChildSpec(
                    goal=(
                        "写 cart.html 和 checkout.html\n"
                        "## 目标\n"
                        "- /tmp/shop/build/cart.html\n"
                        "- /tmp/shop/build/checkout.html\n"
                        "## 内容\n"
                        "- cart.html 引入 style.css 和 app.js\n"
                        "- checkout.html 链接到 order-success.html"
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
