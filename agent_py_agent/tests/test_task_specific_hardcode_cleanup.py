"""Regression tests for task-specific hardcode cleanup in production defaults."""

from __future__ import annotations

from pathlib import Path

from agent_py_agent.agent.contracts.sample_neutrality import (
    SampleNeutralityRule,
    find_sample_neutrality_violations,
)
from agent_py_agent.agent.subagents.policies import _default_forbidden_write_roots as policy_roots
from agent_py_agent.agent.subagents.policy_checks import (
    _default_forbidden_write_roots as check_roots,
)

REPO_ROOT = Path(__file__).resolve().parents[2]


# LLM: Sample neutrality checker should be driven by structured rules, not task-specific code.
# 函数用途: 验证样板污染扫描器只读调用方传入的文件和 marker，不在实现里内置业务词。
def test_sample_neutrality_checker_uses_caller_supplied_rules(tmp_path) -> None:
    clean = tmp_path / "tool_spec.py"
    clean.write_text('example = {"columns": ["记录ID"]}\n', encoding="utf-8")
    dirty = tmp_path / "runtime_prompt.py"
    dirty.write_text('example = {"columns": ["订单ID"]}\n', encoding="utf-8")

    findings = find_sample_neutrality_violations(
        tmp_path,
        [
            SampleNeutralityRule(
                path="tool_spec.py",
                disallowed_markers=("订单ID",),
                surface="tool_prompt_example",
            ),
            SampleNeutralityRule(
                path="runtime_prompt.py",
                disallowed_markers=("订单ID",),
                surface="runtime_prompt_example",
            ),
        ],
    )

    assert [finding.path for finding in findings] == ["runtime_prompt.py"]
    assert findings[0].marker == "订单ID"
    assert findings[0].surface == "runtime_prompt_example"


# LLM: Production defaults should not retain the old shop-specific parent oracle module.
# 函数用途: 防止电商专用父级验收模块重新进入 agent 生产代码路径。
def test_shop_parent_oracle_module_removed_from_production_defaults() -> None:
    assert not (REPO_ROOT / "agent_py_agent/agent/subagents/shop_web_parent_oracle.py").exists()


# LLM: Built-in routing and orchestration examples should stay domain-neutral.
# 函数用途: 只检查审计指出的默认提示/示例文件，避免购物类示例再次污染通用能力路由。
def test_default_tool_examples_do_not_contain_shop_specific_terms() -> None:
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
    findings = find_sample_neutrality_violations(
        REPO_ROOT,
        [
            SampleNeutralityRule(
                path=relative_path,
                disallowed_markers=tuple(sorted(forbidden_terms)),
                surface="runtime_prompt_example",
            )
            for relative_path in (
                "agent_py_agent/agent/capability/router.py",
                "agent_py_agent/agent/agent_core/orchestration_tool_specs.py",
                "agent_py_agent/agent/agent_core/subagent_message_tool.py",
            )
        ],
    )

    assert findings == []


# LLM: Tool prompt examples and contract fixtures should use neutral labels.
# 函数用途: 防止工具示例或 contracts 夹具继续把订单/商品/购物流程写成默认样板。
def test_structured_tool_and_contract_fixtures_use_neutral_sample_terms() -> None:
    findings = find_sample_neutrality_violations(
        REPO_ROOT,
        [
            SampleNeutralityRule(
                path="agent_py_agent/agent/tooling/structured_json_writer.py",
                disallowed_markers=("订单ID", "ORD-"),
                surface="tool_prompt_example",
            ),
            SampleNeutralityRule(
                path="agent_py_agent/agent/contracts/medium_real_acceptance_runner.py",
                disallowed_markers=("cart.html", "checkout.html", ">Pay<", "Pay</button>"),
                surface="contract_fixture",
            ),
            SampleNeutralityRule(
                path="agent_py_agent/agent/contracts/main_agent_foundation_runner.py",
                disallowed_markers=("productGrid", "商品"),
                surface="contract_fixture",
            ),
        ],
    )

    assert findings == []


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
