"""LLM: focused tests for parent-to-child hierarchy contracts.

函数/模块用途: 验证父级明确给出的文件名、层级链路和约束会稳定传给下层，不被模型缩写或跳层。
"""

from __future__ import annotations

from agent_py_agent.agent.subagents.manager import SubAgentManager
from agent_py_agent.agent.subagents.required_file_terms import (
    forbidden_file_terms_from_text,
    required_file_terms_from_text,
)
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
        goal=(
            f"在 {build} 里产出静态购物网站，必须包含 index.html、register.html、login.html、"
            "products.html、product-detail.html、cart.html、checkout.html、order-success.html、style.css、app.js。"
            "本轮必须至少覆盖一条 4 层链路：root -> 子 -> 孙 -> 孙孙。"
        ),
        thought="root only dispatches.",
        plan=["plan"],
        extra_write_roots=[str(build)],
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


# LLM: test_hierarchy_file_contract_skips_forbidden_rename_targets guards R22 prompt corruption.
# 函数用途: 父级写“禁止改成 product.html”时，只继承 product-detail.html，不能把反例当必需产物。
def test_hierarchy_file_contract_skips_forbidden_rename_targets(tmp_path):
    manager = SubAgentManager(tmp_path / "subs")
    build = tmp_path / "deliverables" / "shop" / "build"
    root = manager.create_run(
        goal=(
            f"在 {build} 交付购物站。必须包含 index.html、products.html、product-detail.html、style.css、app.js。"
            "不允许把 product-detail.html 改名成 product.html 或 old-product.html。"
            "不得改名为 legacy.html。"
        ),
        thought="root",
        plan=["plan"],
        extra_write_roots=[str(build)],
    )

    result = manager.schedule_child_runs(
        params=HierarchyScheduleRequest(
            parent_run_id=root.id,
            child_specs=[HierarchyChildSpec(goal=f"继续页面分工，产物目录 {build}", role="child_coordinator")],
            apply=True,
        )
    )
    child = manager.load(result.created_run_ids[0])
    required_section = child.goal.split("父级禁止文件/反例名", 1)[0]

    assert "\n- product-detail.html\n" in required_section
    assert "\n- product.html\n" not in required_section
    assert "\n- old-product.html\n" not in required_section
    assert "\n- legacy.html\n" not in required_section
    assert "父级禁止文件/反例名" in child.goal
    assert "\n- product.html\n" in child.goal
    assert "\n- old-product.html\n" in child.goal
    assert "\n- legacy.html\n" in child.goal


# LLM: test_file_contract_extracts_required_and_forbidden_terms_separately locks the R27 root cause.
# 函数用途: 父级 prompt 同时包含必需文件和禁止反例时，结构化提取要把两类文件分开。
def test_file_contract_extracts_required_and_forbidden_terms_separately():
    text = (
        "必须包含 index.html、products.html、product-detail.html、style.css、app.js。"
        "不允许把 product-detail.html 改名成 product.html 或 old-product.html；"
        "不得改名为 legacy.html，也不要创建 obsolete.html。"
    )

    required = required_file_terms_from_text(text, extensions=r"html?|css|js")
    forbidden = forbidden_file_terms_from_text(text, extensions=r"html?|css|js")

    assert required == ["index.html", "products.html", "product-detail.html", "style.css", "app.js"]
    assert forbidden == ["product.html", "old-product.html", "legacy.html", "obsolete.html"]


# LLM: Parent examples like "禁止改名（如 x.html）" must not become required deliverables.
# 函数用途: 防止 root 用括号举 forbidden 文件名反例时，把 product.html/legacy.html 误传成下级必需文件。
def test_file_contract_treats_negative_examples_as_forbidden_terms():
    text = (
        "必须包含 index.html、products.html、product-detail.html、style.css、app.js。"
        "文件名禁止改名（如 product.html、old-detail.html、legacy.html 等均不允许）。"
    )

    required = required_file_terms_from_text(text, extensions=r"html?|css|js")
    forbidden = forbidden_file_terms_from_text(text, extensions=r"html?|css|js")

    assert required == ["index.html", "products.html", "product-detail.html", "style.css", "app.js"]
    assert forbidden == ["product.html", "old-detail.html", "legacy.html"]


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

    blocked = manager.schedule_child_runs(
        params=HierarchyScheduleRequest(
            parent_run_id=child.id,
            child_specs=[HierarchyChildSpec(goal="直接写页面", role="worker", agent_name="page-worker")],
            apply=True,
        )
    )

    assert blocked.blocked is True
    assert blocked.reason == "hierarchy_chain_requires_coordinator_until_depth_3"
    assert manager.load(child.id).child_ids == []


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
