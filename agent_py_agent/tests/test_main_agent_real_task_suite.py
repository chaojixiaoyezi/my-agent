"""Focused tests for the controlled main-agent real task suite."""

from __future__ import annotations

import json

import pytest


# LLM: The real task suite should write task prompts and contracts as refs, not inline report bodies.
# 函数用途: 验证主代理真实任务套件以结构化文件描述任务，报告里只放引用、工位和验收摘要。
def test_main_agent_real_task_suite_plan_is_refs_first(tmp_path):
    from agent_py_agent.agent.contracts.main_agent_real_task_suite import (
        MainAgentRealTaskSuiteRequest,
        plan_main_agent_real_task_suite,
    )

    report = plan_main_agent_real_task_suite(
        MainAgentRealTaskSuiteRequest(workspace=tmp_path, max_workers=2, task_timeout_seconds=444)
    )

    payload = report.to_dict()
    first_case = payload["cases"][0]
    prompt_path = tmp_path / first_case["prompt_ref"]
    acceptance_path = tmp_path / first_case["acceptance_ref"]
    artifacts_path = tmp_path / first_case["expected_artifacts_ref"]
    acceptance = json.loads(acceptance_path.read_text(encoding="utf-8"))
    assert payload["ok"] is True
    assert payload["summary"]["total"] >= 4
    assert payload["summary"]["planned"] == payload["summary"]["total"]
    assert prompt_path.exists()
    assert artifacts_path.exists()
    assert acceptance["schema_version"] == "main-agent-real-task-acceptance.v1"
    assert "required_artifact_ids" in acceptance
    assert "高端现代家具" not in json.dumps(payload, ensure_ascii=False)
    assert "高端现代家具" in prompt_path.read_text(encoding="utf-8")


# LLM: Worker slot and timeout limits are execution controls, so invalid values fail before any model call.
# 函数用途: 验证真实任务批量测试入口会拒绝无效并发和超时配置，避免无控制地启动任务。
def test_main_agent_real_task_suite_rejects_invalid_controls(tmp_path):
    from agent_py_agent.agent.contracts.main_agent_real_task_suite import (
        MainAgentRealTaskSuiteRequest,
        plan_main_agent_real_task_suite,
    )

    with pytest.raises(ValueError, match="max_workers"):
        plan_main_agent_real_task_suite(
            MainAgentRealTaskSuiteRequest(workspace=tmp_path, max_workers=0)
        )
    with pytest.raises(ValueError, match="task_timeout_seconds"):
        plan_main_agent_real_task_suite(
            MainAgentRealTaskSuiteRequest(workspace=tmp_path, task_timeout_seconds=0)
        )
