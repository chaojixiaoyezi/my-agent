"""Focused tests for the main-agent foundation test runner."""

from __future__ import annotations


# LLM: Main foundation runner should expose the six requested categories without model calls by default.
# 函数用途: 验证主代理基础测试矩阵有固定入口；本地可确定的用例直接执行，真实模型用例明确跳过。
def test_main_agent_foundation_runner_reports_six_categories(tmp_path):
    from agent_py_agent.agent.contracts.main_agent_foundation_runner import (
        MainAgentFoundationRequest,
        run_main_agent_foundation,
    )

    report = run_main_agent_foundation(MainAgentFoundationRequest(workspace=tmp_path))

    assert report.ok is True
    assert report.summary["total"] == 6
    assert report.summary["failed"] == 0
    by_id = {item.case_id: item for item in report.results}
    assert by_id["tool_failure_contracts"].status == "PASSED"
    assert by_id["large_output_artifact_refs"].status == "PASSED"
    assert by_id["deterministic_e2e_matrix"].status == "PASSED"
    assert by_id["single_agent_real_tasks"].status == "SKIPPED"
    assert by_id["compact_resume_real_cycle"].status == "SKIPPED"
    assert by_id["tool_error_recovery_real"].status == "SKIPPED"


# LLM: Foundation reports must stay refs-first so real testing cannot flood prompts with artifact bodies.
# 函数用途: 确认主代理基础测试报告只返回摘要和证据路径，不把大工具输出正文塞回 JSON。
def test_main_agent_foundation_report_is_refs_first(tmp_path):
    from agent_py_agent.agent.contracts.main_agent_foundation_runner import (
        MainAgentFoundationRequest,
        run_main_agent_foundation,
    )

    payload = run_main_agent_foundation(MainAgentFoundationRequest(workspace=tmp_path)).to_dict()

    assert payload["summary"]["total"] == 6
    assert "large output row" not in str(payload)
    large_case = next(item for item in payload["results"] if item["case_id"] == "large_output_artifact_refs")
    assert large_case["evidence_refs"]


# LLM: Tool failure contracts need stable categories before real-model recovery tests are meaningful.
# 函数用途: 覆盖路径、权限、超时、工具不可用和模型上游失败，保证主代理后续能按错误类型恢复。
def test_main_agent_foundation_tool_failure_contracts_are_specific(tmp_path):
    from agent_py_agent.agent.contracts.main_agent_foundation_runner import (
        MainAgentFoundationRequest,
        run_main_agent_foundation,
    )

    report = run_main_agent_foundation(MainAgentFoundationRequest(workspace=tmp_path))
    item = next(result for result in report.results if result.case_id == "tool_failure_contracts")

    assert item.status == "PASSED"
    assert not item.issues
    evidence = item.evidence_refs[0]
    text = tmp_path.joinpath(evidence).read_text(encoding="utf-8") if not evidence.startswith("/") else __import__("pathlib").Path(evidence).read_text(encoding="utf-8")
    assert "PATH_INVALID" in text
    assert "TOOL_TIMEOUT" in text
    assert "MODEL_UPSTREAM_FAILED" in text
