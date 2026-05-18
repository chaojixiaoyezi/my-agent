"""LLM: focused tests for parent-to-child hierarchy contracts.

函数/模块用途: 验证父级明确给出的文件名、层级链路和约束会稳定传给下层，不被模型缩写或跳层。
"""

from __future__ import annotations

from agent_py_agent.agent.subagents.manager import SubAgentManager
from agent_py_agent.agent.subagents.services.hierarchy_scheduler import (
    HierarchyChildSpec,
    HierarchyScheduleRequest,
)


# LLM: test_hierarchy_schedule_preserves_shopping_file_contract covers real E2E page-name drift.
# 函数用途: 购物站这类父级明确列文件名时，下层不能把 product-detail/style.css/app.js 改成其它名字。
def test_hierarchy_schedule_preserves_shopping_file_contract_when_child_goal_only_has_build_dir(tmp_path):
    manager = SubAgentManager(tmp_path / "subs")
    build = tmp_path / "deliverables" / "shop" / "build"
    root = manager.create_run(
        goal="购物站需要完整页面结构和 4 层链路。",
        thought="root only dispatches.",
        plan=["plan"],
        extra_write_roots=[str(build)],
        attributes={
            "required_files": [
                "index.html", "register.html", "login.html", "products.html", "product-detail.html",
                "cart.html", "checkout.html", "order-success.html", "style.css", "app.js",
            ],
            "hierarchy_contracts": ["4 层链路", "depth=1 小傻妞-*", "depth=2 小小傻妞-*", "depth=3 小小小傻妞-*"],
        },
    )

    result = manager.schedule_child_runs(
        params=HierarchyScheduleRequest(
            parent_run_id=root.id,
            child_specs=[
                HierarchyChildSpec(
                    goal=f"创建页面架构 coordinator，产物目录 {build}",
                    role="child_coordinator",
                    agent_name="shop-lead",
                    allowed_tools=["schedule_child_subagents", "dispatch_subagents", "subagent_board"],
                )
            ],
            apply=True,
        )
    )
    child = manager.load(result.created_run_ids[0])

    assert "product-detail.html" in child.goal
    assert "order-success.html" in child.goal
    assert "style.css" in child.goal
    assert "app.js" in child.goal
    assert "4 层链路" in child.goal


# LLM: R44 proved exact forbidden filenames must survive even when a child mentions the build root.
# 函数用途: child goal 已包含产物目录和必需文件时，仍必须继承 product.html/output.json 等具体禁止清单。
def test_hierarchy_schedule_preserves_forbidden_file_contract_when_child_goal_summarizes_constraints(tmp_path):
    manager = SubAgentManager(tmp_path / "subs")
    build = tmp_path / "deliverables" / "shop" / "build"
    root = _forbidden_file_contract_root(manager, build)
    result = _schedule_forbidden_contract_child(manager, root.id, build)
    child = manager.load(result.created_run_ids[0])

    assert child.attributes["forbidden_files"] == [
        "product.html", "old-product.html", "legacy.html", "obsolete.html",
        "output.json", "RUNNER_RESULT.md", "execution_context.json",
    ]


def _forbidden_file_contract_root(manager: SubAgentManager, build):
    return manager.create_run(
        goal="购物站需要完整页面结构，并禁止旧文件名。",
        thought="root only dispatches.",
        plan=["plan"],
        extra_write_roots=[str(build)],
        attributes={
            "required_files": [
                "index.html", "register.html", "login.html", "products.html", "product-detail.html",
                "cart.html", "checkout.html", "order-success.html", "style.css", "app.js",
            ],
            "forbidden_files": [
                "product.html", "old-product.html", "legacy.html", "obsolete.html",
                "output.json", "RUNNER_RESULT.md", "execution_context.json",
            ],
        },
    )


def _schedule_forbidden_contract_child(manager: SubAgentManager, root_id: str, build):
    return manager.schedule_child_runs(params=HierarchyScheduleRequest(
        parent_run_id=root_id,
        child_specs=[HierarchyChildSpec(
            goal=(
                f"交付静态购物网站页面到 {build}。核心产物必须同名：index.html、register.html、"
                "login.html、products.html、product-detail.html、cart.html、checkout.html、"
                "order-success.html、style.css、app.js。约束：禁止文件名改、禁止 output.json。"
            ),
            role="child_coordinator",
            agent_name="小傻妞-页面协调",
            allowed_tools=["schedule_child_subagents", "dispatch_subagents"],
        )],
        apply=True,
    ))


# LLM: R70 showed schedule-time chain forcing over-constrained otherwise valid implementation children.
# 函数用途: 父级写 4层/depth=3/小傻妞命名规则时，下级必须继续携带；调度阶段不再硬挡 worker/leaf，最终链路交给验收判断。
def test_hierarchy_schedule_preserves_no_space_four_layer_contract_without_forcing_coord_chain(tmp_path):
    manager = SubAgentManager(tmp_path / "subs")
    build = tmp_path / "deliverables" / "shop" / "build"
    child = _create_no_space_four_layer_child(manager, build)

    assert "hierarchy_contracts:" in child.goal
    assert "4层链路要求" in child.goal
    assert "depth=3" in child.goal

    result = manager.schedule_child_runs(
        params=HierarchyScheduleRequest(
            parent_run_id=child.id,
            child_specs=[HierarchyChildSpec(goal="直接写完整页面", role="leaf_worker", agent_name="小小傻妞-页面编写员")],
            apply=True,
        )
    )

    assert result.blocked is False
    leaf = manager.load(result.created_run_ids[0])
    assert "4层链路要求" in leaf.goal
    assert "小小小傻妞-*" in leaf.goal


# LLM: _create_no_space_four_layer_child keeps the no-space hierarchy regression focused on assertions.
# 函数用途: 创建带“4层”中文无空格约束的 root 和第一层 coordinator。
def _create_no_space_four_layer_child(manager: SubAgentManager, build):
    root = manager.create_run(
        goal="购物站需要指定文件和 4 层链路。",
        thought="root only dispatches.",
        plan=["plan"],
        extra_write_roots=[str(build)],
        attributes={
            "required_files": ["index.html", "products.html", "product-detail.html", "style.css", "app.js"],
            "hierarchy_contracts": [
                "4层链路要求", "depth=1 小傻妞-*", "depth=2 小小傻妞-*", "depth=3 小小小傻妞-*", "max_depth=3",
            ],
        },
    )
    child_result = manager.schedule_child_runs(
        params=HierarchyScheduleRequest(
            parent_run_id=root.id,
            child_specs=[
                HierarchyChildSpec(
                    goal=(
                        f"创建页面架构 coordinator，产物目录 {build}，核心产物 index.html、products.html、"
                        "product-detail.html、style.css、app.js。"
                    ),
                    role="child_coordinator",
                    agent_name="小傻妞-页面协调",
                    allowed_tools=["schedule_child_subagents", "dispatch_subagents"],
                )
            ],
            apply=True,
        )
    )
    return manager.load(child_result.created_run_ids[0])


# LLM: test_hierarchy_file_contract_skips_forbidden_rename_targets guards R22 prompt corruption.
# 函数用途: 父级写“禁止改成 product.html”时，只继承 product-detail.html，不能把反例当必需产物。
def test_hierarchy_file_contract_skips_forbidden_rename_targets(tmp_path):
    manager = SubAgentManager(tmp_path / "subs")
    build = tmp_path / "deliverables" / "shop" / "build"
    root = manager.create_run(
        goal="购物站需要指定文件，并禁止旧文件名。",
        thought="root",
        plan=["plan"],
        extra_write_roots=[str(build)],
        attributes={
            "required_files": ["index.html", "products.html", "product-detail.html", "style.css", "app.js"],
            "forbidden_files": ["product.html", "old-product.html", "legacy.html"],
        },
    )

    result = manager.schedule_child_runs(
        params=HierarchyScheduleRequest(
            parent_run_id=root.id,
            child_specs=[HierarchyChildSpec(goal=f"继续页面分工，产物目录 {build}", role="child_coordinator")],
            apply=True,
        )
    )
    child = manager.load(result.created_run_ids[0])
    required_section = child.goal.split("forbidden_files:", 1)[0]

    assert "\n- product-detail.html\n" in required_section
    assert "\n- product.html\n" not in required_section
    assert "\n- old-product.html\n" not in required_section
    assert "\n- legacy.html\n" not in required_section
    assert "forbidden_files:" in child.goal
    assert "\n- product.html\n" in child.goal
    assert "\n- old-product.html\n" in child.goal
    assert "\n- legacy.html" in child.goal

# LLM: test_hierarchy_schedule_blocks_leaf_before_explicit_four_layer_chain_reaches_depth_three covers root-only E2E.
# 函数用途: 父级明确要求 4 层链路时，深度未到孙孙层前不能直接创建 leaf/worker 跳层。
def test_hierarchy_schedule_blocks_leaf_before_explicit_four_layer_chain_reaches_depth_three(tmp_path):
    manager = SubAgentManager(tmp_path / "subs")
    root = manager.create_run(
        goal="本轮必须至少覆盖一条 4 层链路：root -> 子 -> 孙 -> 孙孙。",
        thought="root",
        plan=["plan"],
    )
    child_result = manager.schedule_child_runs(
        params=HierarchyScheduleRequest(
            parent_run_id=root.id,
            child_specs=[HierarchyChildSpec(goal="继续协调", role="child_coordinator", agent_name="lead")],
            apply=True,
        )
    )
    child = manager.load(child_result.created_run_ids[0])

    result = manager.schedule_child_runs(
        params=HierarchyScheduleRequest(
            parent_run_id=child.id,
            child_specs=[HierarchyChildSpec(goal="直接写页面", role="worker", agent_name="page-worker")],
            apply=True,
        )
    )

    assert result.blocked is False
    assert manager.load(child.id).child_ids == result.created_run_ids


# LLM: Coordinator names can include writer/domain words without becoming leaf workers.
# 函数用途: 防止“小小傻妞-site-writer”这类 coordinator 因名字里有 writer 被四层链路 guard 误挡。
def test_hierarchy_schedule_allows_coordinator_name_with_writer_before_depth_three(tmp_path):
    manager = SubAgentManager(tmp_path / "subs")
    root = manager.create_run(
        goal="本轮必须至少覆盖一条 4 层链路：root -> 子 -> 孙 -> 孙孙。",
        thought="root",
        plan=["plan"],
    )
    child_result = manager.schedule_child_runs(
        params=HierarchyScheduleRequest(
            parent_run_id=root.id,
            child_specs=[
                HierarchyChildSpec(goal="继续协调", role="coordinator", agent_name="小傻妞-html-coordinator")
            ],
            apply=True,
        )
    )
    child = manager.load(child_result.created_run_ids[0])

    result = manager.schedule_child_runs(
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
