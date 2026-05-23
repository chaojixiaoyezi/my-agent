"""Regression tests for task-specific hardcode cleanup in production defaults."""

from __future__ import annotations

from pathlib import Path

from agent_py_agent.agent.subagents.policies import _default_forbidden_write_roots as policy_roots
from agent_py_agent.agent.subagents.policy_checks import (
    _default_forbidden_write_roots as check_roots,
)

REPO_ROOT = Path(__file__).resolve().parents[2]


# LLM: Production defaults should not retain the old shop-specific parent oracle module.
# 函数用途: 防止电商专用父级验收模块重新进入 agent 生产代码路径。
def test_shop_parent_oracle_module_removed_from_production_defaults() -> None:
    assert not (REPO_ROOT / "agent_py_agent/agent/subagents/shop_web_parent_oracle.py").exists()


# LLM: Built-in routing and orchestration examples should stay domain-neutral.
# 函数用途: 只检查审计指出的默认提示/示例文件，避免购物类示例再次污染通用能力路由。
def test_default_tool_examples_do_not_contain_shop_specific_terms() -> None:
    checked_files = [
        "agent_py_agent/agent/capability/router.py",
        "agent_py_agent/agent/agent_core/orchestration_tool_specs.py",
        "agent_py_agent/agent/agent_core/subagent_message_tool.py",
    ]
    forbidden_terms = {
        "购物",
        "购物车",
        "商品目录",
        "下单",
        "catalog-lead",
        "child-auth",
        "child-catalog",
        "add-to-cart",
        "place-order",
        "测试注册、登录",
    }

    for relative_path in checked_files:
        text = (REPO_ROOT / relative_path).read_text(encoding="utf-8")
        assert not (forbidden_terms & {term for term in forbidden_terms if term in text}), relative_path


# LLM: Default hierarchy stopwords should not include user-specific or one-task domain names.
# 函数用途: 确认领域去重词表不再把个人名或购物任务样本写成生产默认。
def test_hierarchy_domain_stopwords_are_user_and_task_neutral() -> None:
    from agent_py_agent.agent.subagents.services.hierarchy_domain_terms import DOMAIN_STOPWORDS

    assert "xiaoyezi" not in DOMAIN_STOPWORDS
    assert "shop" not in DOMAIN_STOPWORDS


# LLM: Reference-project write protection should be configured, not hardcoded into defaults.
# 函数用途: 防止默认 forbidden roots 写死 .通道运行时 这类外部项目路径。
def test_default_forbidden_write_roots_do_not_hardcode_reference_project_dirs() -> None:
    for roots in (policy_roots(), check_roots()):
        assert all(not str(root).endswith(".openclaw") for root in roots)
