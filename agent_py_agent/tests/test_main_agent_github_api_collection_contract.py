"""GitHub workbook task API collection contracts."""

from __future__ import annotations

import json
from pathlib import Path


# LLM: GitHub workbook real task should expose an executable source collection plan.
# 函数用途: 验证复杂真实任务的采集范围和字段映射落在结构化合同里，而不是靠 prompt 猜。
def test_real_task_github_contract_declares_api_collection_plan(tmp_path: Path) -> None:
    from agent_py_agent.agent.contracts.main_agent_real_task_suite import (
        MainAgentRealTaskSuiteRequest,
        plan_main_agent_real_task_suite,
    )

    report = plan_main_agent_real_task_suite(MainAgentRealTaskSuiteRequest(workspace=tmp_path, max_workers=1))
    api_request = _api_request_from_expected_ref(tmp_path, report.cases)

    assert api_request["request_ranges"]
    assert api_request["request_ranges"][0]["start_date"].endswith("-01-01")
    assert api_request["request_ranges"][0]["step_days"] == 7
    assert api_request["request_ranges"][0]["reserved"]["metric_kind"] == "time_window_delta"
    assert api_request["request_ranges"][0]["reserved"]["window_start"] == "{start_date}"
    assert "url_template" in api_request["request_ranges"][0]
    assert api_request["fields"]["项目名"] == "full_name"
    assert api_request["fields"]["中文解释"]["default_template"]
    assert "上升 star 数" in api_request["evidence_fields"]


# LLM: Main task suite mirrors the same structured GitHub collection contract as the real suite.
# 函数用途: 验证 real-e2e 默认使用的主任务套件不会丢失 api_request 采集计划。
def test_main_task_github_contract_declares_api_collection_plan(tmp_path: Path) -> None:
    from agent_py_agent.agent.contracts.main_agent_task_suite import (
        MainAgentTaskSuiteRequest,
        plan_main_agent_task_suite,
    )

    report = plan_main_agent_task_suite(MainAgentTaskSuiteRequest(workspace=tmp_path, max_workers=1))
    api_request = _api_request_from_expected_ref(tmp_path, report.cases)

    assert api_request["request_ranges"]
    assert api_request["request_ranges"][0]["reserved"]["time_window"]["end"] == "{end_date}"
    assert api_request["fields"]["推荐理由"]["template"]


def _api_request_from_expected_ref(tmp_path: Path, cases: object) -> dict[str, object]:
    case = next(item for item in cases if item.case_id == "github_weekly_star_growth_xlsx")
    artifacts = json.loads((tmp_path / case.expected_artifacts_ref).read_text(encoding="utf-8"))
    return artifacts["artifacts"][0]["validation_contract"]["collection_contract"]["api_request"]
