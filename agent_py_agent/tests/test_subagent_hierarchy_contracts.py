"""LLM: focused tests for parent-to-child hierarchy contracts.

函数/模块用途: 验证父级明确给出的文件名、层级链路和约束会稳定传给下层，不被模型缩写或跳层。
"""

from __future__ import annotations

from agent_py_agent.agent.subagents.manager import SubAgentManager
from agent_py_agent.agent.subagents.services.hierarchy.scheduler import (
    HierarchyChildSpec,
    HierarchyScheduleRequest,
)


def test_hierarchy_schedule_preserves_shopping_file_contract_when_child_goal_only_has_build_dir(tmp_path):
    manager = SubAgentManager(tmp_path / "subs")
    build = tmp_path / "deliverables" / "shop" / "build"
    root = manager.create_run(
        goal="示例站需要完整页面结构和 4 层链路。",
        thought="root only dispatches.",
        plan=["plan"],
        extra_write_roots=[str(build)],
        attributes={
            "required_files": [
                "index.html", "register.html", "login.html", "items.html", "item-detail.html",
                "flow-a.html", "flow-b.html", "flow-done.html", "style.css", "app.js",
            ],
            "hierarchy_contracts": ["4 层链路", "depth=1 小傻妞-*", "depth=2 小小傻妞-*", "depth=3 小小小傻妞-*"],
        },
    )

    result = manager.hierarchy.schedule_child_runs(
        params=HierarchyScheduleRequest(
            parent_run_id=root.id,
            child_specs=[
                HierarchyChildSpec(
                    goal=f"创建页面架构 coordinator，产物目录 {build}",
                    role="coordinator",
                    agent_name="shop-lead",
                    allowed_tools=["schedule_child_subagents", "dispatch_subagents", "inspect_agent_tree"],
                )
            ],
            apply=True,
        )
    )
    child = manager.load(result.created_run_ids[0])

    assert "item-detail.html" in child.goal
    assert "flow-done.html" in child.goal
    assert "style.css" in child.goal
    assert "app.js" in child.goal
    assert "4 层链路" in child.goal


def test_hierarchy_schedule_preserves_forbidden_file_contract_when_child_goal_summarizes_constraints(tmp_path):
    manager = SubAgentManager(tmp_path / "subs")
    build = tmp_path / "deliverables" / "shop" / "build"
    root = _forbidden_file_contract_root(manager, build)
    result = _schedule_forbidden_contract_child(manager, root.id, build)
    child = manager.load(result.created_run_ids[0])

    assert child.attributes["forbidden_files"] == [
        "product.html", "old-product.html", "stale.html", "obsolete.html",
        "output.json", "RUNNER_RESULT.md", "execution_context.json",
    ]


def _forbidden_file_contract_root(manager: SubAgentManager, build):
    return manager.create_run(
        goal="示例站需要完整页面结构，并禁止旧文件名。",
        thought="root only dispatches.",
        plan=["plan"],
        extra_write_roots=[str(build)],
        attributes={
            "required_files": [
                "index.html", "register.html", "login.html", "items.html", "item-detail.html",
                "flow-a.html", "flow-b.html", "flow-done.html", "style.css", "app.js",
            ],
            "forbidden_files": [
                "product.html", "old-product.html", "stale.html", "obsolete.html",
                "output.json", "RUNNER_RESULT.md", "execution_context.json",
            ],
        },
    )


def _schedule_forbidden_contract_child(manager: SubAgentManager, root_id: str, build):
    return manager.hierarchy.schedule_child_runs(params=HierarchyScheduleRequest(
        parent_run_id=root_id,
        child_specs=[HierarchyChildSpec(
            goal=(
                f"交付静态示例网站页面到 {build}。核心产物必须同名：index.html、register.html、"
                "login.html、items.html、item-detail.html、flow-a.html、flow-b.html、"
                "flow-done.html、style.css、app.js。约束：禁止文件名改、禁止 output.json。"
            ),
            role="coordinator",
            agent_name="小傻妞-页面协调",
            allowed_tools=["schedule_child_subagents", "dispatch_subagents"],
        )],
        apply=True,
    ))


def test_hierarchy_schedule_preserves_no_space_four_layer_contract_without_forcing_coord_chain(tmp_path):
    manager = SubAgentManager(tmp_path / "subs")
    build = tmp_path / "deliverables" / "shop" / "build"
    child = _create_no_space_four_layer_child(manager, build)

    assert "hierarchy_contracts:" in child.goal
    assert "4层链路要求" in child.goal
    assert "depth=3" in child.goal

    result = manager.hierarchy.schedule_child_runs(
        params=HierarchyScheduleRequest(
            parent_run_id=child.id,
            child_specs=[HierarchyChildSpec(goal="直接写完整页面", role="worker", agent_name="小小傻妞-页面编写员")],
            apply=True,
        )
    )

    assert result.blocked is False
    leaf = manager.load(result.created_run_ids[0])
    assert "4层链路要求" in leaf.goal
    assert "小小小傻妞-*" in leaf.goal


def _create_no_space_four_layer_child(manager: SubAgentManager, build):
    root = manager.create_run(
        goal="示例站需要指定文件和 4 层链路。",
        thought="root only dispatches.",
        plan=["plan"],
        extra_write_roots=[str(build)],
        attributes={
            "required_files": ["index.html", "items.html", "item-detail.html", "style.css", "app.js"],
            "hierarchy_contracts": [
                "4层链路要求", "depth=1 小傻妞-*", "depth=2 小小傻妞-*", "depth=3 小小小傻妞-*", "max_depth=3",
            ],
        },
    )
    child_result = manager.hierarchy.schedule_child_runs(
        params=HierarchyScheduleRequest(
            parent_run_id=root.id,
            child_specs=[
                HierarchyChildSpec(
                    goal=(
                        f"创建页面架构 coordinator，产物目录 {build}，核心产物 index.html、items.html、"
                        "item-detail.html、style.css、app.js。"
                    ),
                    role="coordinator",
                    agent_name="小傻妞-页面协调",
                    allowed_tools=["schedule_child_subagents", "dispatch_subagents"],
                )
            ],
            apply=True,
        )
    )
    return manager.load(child_result.created_run_ids[0])


def test_hierarchy_file_contract_skips_forbidden_rename_targets(tmp_path):
    manager = SubAgentManager(tmp_path / "subs")
    build = tmp_path / "deliverables" / "shop" / "build"
    root = manager.create_run(
        goal="示例站需要指定文件，并禁止旧文件名。",
        thought="root",
        plan=["plan"],
        extra_write_roots=[str(build)],
        attributes={
            "required_files": ["index.html", "items.html", "item-detail.html", "style.css", "app.js"],
            "forbidden_files": ["product.html", "old-product.html", "stale.html"],
        },
    )

    result = manager.hierarchy.schedule_child_runs(
        params=HierarchyScheduleRequest(
            parent_run_id=root.id,
            child_specs=[HierarchyChildSpec(goal=f"继续页面分工，产物目录 {build}", role="coordinator")],
            apply=True,
        )
    )
    child = manager.load(result.created_run_ids[0])
    required_section = child.goal.split("forbidden_files:", 1)[0]

    assert "\n- item-detail.html\n" in required_section
    assert "\n- product.html\n" not in required_section
    assert "\n- old-product.html\n" not in required_section
    assert "\n- stale.html\n" not in required_section
    assert "forbidden_files:" in child.goal
    assert "\n- product.html\n" in child.goal
    assert "\n- old-product.html\n" in child.goal
    assert "\n- stale.html" in child.goal

def test_hierarchy_schedule_blocks_leaf_before_explicit_four_layer_chain_reaches_depth_three(tmp_path):
    manager = SubAgentManager(tmp_path / "subs")
    root = manager.create_run(
        goal="本轮必须至少覆盖一条 4 层链路：root -> 子 -> 孙 -> 孙孙。",
        thought="root",
        plan=["plan"],
    )
    child_result = manager.hierarchy.schedule_child_runs(
        params=HierarchyScheduleRequest(
            parent_run_id=root.id,
            child_specs=[HierarchyChildSpec(goal="继续协调", role="coordinator", agent_name="lead")],
            apply=True,
        )
    )
    child = manager.load(child_result.created_run_ids[0])

    result = manager.hierarchy.schedule_child_runs(
        params=HierarchyScheduleRequest(
            parent_run_id=child.id,
            child_specs=[HierarchyChildSpec(goal="直接写页面", role="worker", agent_name="page-worker")],
            apply=True,
        )
    )

    assert result.blocked is False
    assert manager.load(child.id).child_ids == result.created_run_ids


def test_hierarchy_schedule_allows_coordinator_name_with_writer_before_depth_three(tmp_path):
    manager = SubAgentManager(tmp_path / "subs")
    root = manager.create_run(
        goal="本轮必须至少覆盖一条 4 层链路：root -> 子 -> 孙 -> 孙孙。",
        thought="root",
        plan=["plan"],
    )
    child_result = manager.hierarchy.schedule_child_runs(
        params=HierarchyScheduleRequest(
            parent_run_id=root.id,
            child_specs=[
                HierarchyChildSpec(goal="继续协调", role="coordinator", agent_name="小傻妞-html-coordinator")
            ],
            apply=True,
        )
    )
    child = manager.load(child_result.created_run_ids[0])

    result = manager.hierarchy.schedule_child_runs(
        params=HierarchyScheduleRequest(
            parent_run_id=child.id,
            child_specs=[
                HierarchyChildSpec(
                    goal="继续拆解页面编写任务，后续再创建 depth=3 leaf worker。",
                    role="coordinator",
                    agent_name="小小傻妞-site-writer",
                )
            ],
            apply=True,
        )
    )

    assert result.blocked is False
    assert result.created_run_ids
    grandchild = manager.load(result.created_run_ids[0])
    assert grandchild.depth == 2
    assert "coordinator" in grandchild.role
