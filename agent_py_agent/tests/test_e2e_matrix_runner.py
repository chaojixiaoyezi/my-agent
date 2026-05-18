"""Focused tests for the deterministic Real E2E Matrix runner."""

from __future__ import annotations


# LLM: Deterministic E2E runner should execute local checks and skip real-model cases by default.
# 函数用途: 验证真实 E2E 矩阵有可执行入口，先跑不花模型调用的用例，并明确跳过真实模型用例。
def test_e2e_matrix_runner_executes_deterministic_cases_and_skips_real_model(tmp_path):
    from agent_py_agent.agent.contracts.e2e_matrix_runner import E2ERunnerRequest, run_e2e_matrix

    report = run_e2e_matrix(E2ERunnerRequest(workspace=tmp_path))

    assert report.ok is True
    assert report.summary["passed"] >= 3
    assert report.summary["failed"] == 0
    assert report.summary["skipped"] >= 1
    by_id = {item.case_id: item for item in report.results}
    assert by_id["windows_chinese_path_write"].status == "PASSED"
    assert by_id["large_tool_output_artifact"].status == "PASSED"
    assert by_id["tool_failure_taxonomy"].status == "PASSED"
    assert by_id["compact_resume_continue"].status == "SKIPPED"


# LLM: Runner output must be serializable for CLI/frontend/reporting without reading big artifacts.
# 函数用途: 确认报告输出只包含摘要、证据引用和状态，不把大文件正文塞回 JSON。
def test_e2e_matrix_report_to_dict_is_refs_first(tmp_path):
    from agent_py_agent.agent.contracts.e2e_matrix_runner import E2ERunnerRequest, run_e2e_matrix

    report = run_e2e_matrix(E2ERunnerRequest(workspace=tmp_path))
    payload = report.to_dict()

    assert payload["ok"] is True
    assert payload["summary"]["total"] == len(payload["results"])
    artifact = next(item for item in payload["results"] if item["case_id"] == "large_tool_output_artifact")
    assert artifact["evidence_refs"]
    assert "large output line" not in str(payload)
