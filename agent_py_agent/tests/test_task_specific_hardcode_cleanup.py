"""Regression tests for task-specific hardcode cleanup in production defaults."""

from __future__ import annotations

from pathlib import Path

from agent_py_agent.agent.contracts.sample_neutrality import (
    SampleNeutralityRule,
    find_sample_neutrality_violations,
)
from agent_py_agent.agent.subagents.policies import _default_forbidden_write_roots as policy_roots

REPO_ROOT = Path(__file__).resolve().parents[2]


def test_sample_neutrality_checker_uses_caller_supplied_rules(tmp_path) -> None:
    clean = tmp_path / "tool_spec.py"
    clean.write_text('example = {"columns": ["字段A"]}\n', encoding="utf-8")
    dirty = tmp_path / "runtime_prompt.py"
    dirty.write_text('example = {"columns": ["记录ID"]}\n', encoding="utf-8")

    findings = find_sample_neutrality_violations(
        tmp_path,
        [
            SampleNeutralityRule(
                path="tool_spec.py",
                disallowed_markers=("记录ID",),
                surface="tool_prompt_example",
            ),
            SampleNeutralityRule(
                path="runtime_prompt.py",
                disallowed_markers=("记录ID",),
                surface="runtime_prompt_example",
            ),
        ],
    )

    assert [finding.path for finding in findings] == ["runtime_prompt.py"]
    assert findings[0].marker == "记录ID"
    assert findings[0].surface == "runtime_prompt_example"


def test_shop_parent_oracle_module_removed_from_production_defaults() -> None:
    assert not (REPO_ROOT / "agent_py_agent/agent/subagents/shop_web_parent_oracle.py").exists()


def test_default_tool_examples_do_not_contain_shop_specific_terms() -> None:
    forbidden_terms = {
        "示例流程",
        "流程状态",
        "条目目录",
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
                "agent_py_agent/agent/agent_core/orchestration/tool_specs.py",
            )
        ],
    )

    assert findings == []


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
                disallowed_markers=("flow-a.html", "flow-b.html", ">Pay<", "Pay</button>"),
                surface="contract_fixture",
            ),
            SampleNeutralityRule(
                path="agent_py_agent/agent/contracts/main_agent_foundation_runner.py",
                disallowed_markers=("productGrid", "条目"),
                surface="contract_fixture",
            ),
        ],
    )

    assert findings == []


def test_hierarchy_domain_stopwords_are_user_and_task_neutral() -> None:
    from agent_py_agent.agent.subagents.services.hierarchy.domain_terms import DOMAIN_STOPWORDS

    assert "xiaoyezi" not in DOMAIN_STOPWORDS
    assert "shop" not in DOMAIN_STOPWORDS


def test_default_forbidden_write_roots_do_not_hardcode_reference_project_dirs() -> None:
    assert all(not str(root).endswith(".openclaw") for root in policy_roots())
