"""Tests for the generic data-analysis main-agent task package."""

from __future__ import annotations

import json
from pathlib import Path


# LLM: analysis package tasks need multi-artifact gates before expensive model runs.
# 函数用途: 验证通用数据分析包同时声明源数据、工作簿、PDF 报告和 HTML 仪表盘的机器合同。
def test_data_analysis_package_has_generic_multi_artifact_contract(tmp_path):
    case, artifacts = _data_analysis_case_artifacts(tmp_path)
    by_id = {item["artifact_id"]: item for item in artifacts["artifacts"]}

    assert case.case_id == "data_analysis_package"
    assert set(by_id) == {
        "analysis_source_data",
        "analysis_workbook",
        "analysis_pdf_report",
        "analysis_html_dashboard",
    }
    assert by_id["analysis_source_data"]["validation_contract"]["required_fields"] == ["rows"]
    assert by_id["analysis_workbook"]["validation_contract"]["collection_contract"][
        "min_items_total"
    ] >= 1000
    assert by_id["analysis_workbook"]["validation_contract"]["staging_contract"][
        "builder_tool"
    ] == "data_to_workbook"
    assert "generated_rows" in by_id["analysis_workbook"]["validation_contract"]["staging_contract"][
        "checkpoint_shape_hints"
    ]["outputs/data_analysis/source_data.json"]
    assert by_id["analysis_pdf_report"]["validation_contract"]["staging_contract"][
        "builder_tool"
    ] == "markdown_to_pdf"
    assert by_id["analysis_html_dashboard"]["validation_contract"]["quality_requirements"][
        "complete_html_document"
    ] is True


# LLM: controlled real tasks pass one ordinary prompt plus separate machine contracts.
# 函数用途: 防止真实任务 runner 退化成把合同塞进 prompt 或靠多轮人工提示补充机器事实。
def test_command_uses_single_prompt_and_separate_delivery_contract(tmp_path):
    from agent_py_agent.agent.contracts.main_agent_task_execution_files import (
        case_paths,
        command_for_case,
    )
    from agent_py_agent.agent.contracts.main_agent_task_execution_models import (
        MainAgentTaskExecutionRequest,
    )

    for case in _selected_complex_cases(tmp_path):
        paths = case_paths(tmp_path, case.case_id)
        command = command_for_case(
            case,
            MainAgentTaskExecutionRequest(workspace=tmp_path),
            config_path=tmp_path / f"{case.case_id}.yaml",
            workspace=tmp_path,
            delivery_contract_path=paths["delivery_contract"],
        )
        run_index = command.index("run")
        contract_flag_index = command.index("--delivery-contract-file")
        payload = json.loads(paths["delivery_contract"].read_text(encoding="utf-8"))

        assert contract_flag_index == run_index + 2
        assert "--inject" not in command
        assert "--prompt-file" not in command
        assert payload["case_id"] == case.case_id
        assert payload["artifacts"]
        assert payload["bootstrap_contract"]["materialization_targets"]


# LLM: data package gates must reject partial source rows even when a workbook exists.
# 函数用途: 复现真实任务“只有几百行却声称一千行”的问题，验收只看结构化 rows 数量。
def test_data_analysis_rejects_partial_source_rows(tmp_path):
    from agent_py_agent.agent.contracts.artifact_acceptance import (
        ArtifactAcceptanceRequest,
        validate_artifact,
    )
    from agent_py_agent.agent.tooling.spreadsheet_builder import DataWorkbookTool

    _, artifacts = _data_analysis_case_artifacts(tmp_path)
    workbook = next(item for item in artifacts["artifacts"] if item["artifact_id"] == "analysis_workbook")
    _write_source_data(tmp_path / "outputs/data_analysis/source_data.json", _analysis_rows(576))
    result = DataWorkbookTool(tmp_path).execute(
        {
            "source_json_path": "outputs/data_analysis/source_data.json",
            "path": "outputs/data_analysis/analysis.xlsx",
        }
    )

    report = validate_artifact(
        ArtifactAcceptanceRequest(
            path=tmp_path / "outputs/data_analysis/analysis.xlsx",
            workspace_root=tmp_path,
            validation_contract=workbook["validation_contract"],
        )
    )
    codes = {item.code for item in report.findings}

    assert result.ok is True
    assert report.ok is False
    assert "COLLECTION_TOO_FEW_ITEMS" in codes


# LLM: one generated structured source should be enough for the generic workbook gate.
# 函数用途: 验证大表不需要模型手写一千行；机器生成规则产物仍必须通过同一 workbook 验收门。
def test_data_analysis_accepts_generated_source_rows(tmp_path):
    from agent_py_agent.agent.contracts.artifact_acceptance import (
        ArtifactAcceptanceRequest,
        validate_artifact,
    )
    from agent_py_agent.agent.tooling.spreadsheet_builder import DataWorkbookTool
    from agent_py_agent.agent.tooling.structured_json_writer import StructuredJsonTool

    _, artifacts = _data_analysis_case_artifacts(tmp_path)
    workbook = next(item for item in artifacts["artifacts"] if item["artifact_id"] == "analysis_workbook")
    source = StructuredJsonTool(tmp_path).execute(_generated_analysis_source_call())
    result = DataWorkbookTool(tmp_path).execute(
        {
            "source_json_path": "outputs/data_analysis/source_data.json",
            "path": "outputs/data_analysis/analysis.xlsx",
        }
    )

    report = validate_artifact(
        ArtifactAcceptanceRequest(
            path=tmp_path / "outputs/data_analysis/analysis.xlsx",
            workspace_root=tmp_path,
            validation_contract=workbook["validation_contract"],
        )
    )

    assert source.ok, source.output
    assert result.ok, result.output
    assert report.ok, [item.code for item in report.findings]


# LLM: _selected_complex_cases returns the four target complex tasks by structured case_id.
# 函数用途: 只用 case_id 选择任务，不读取 prompt 文本或自然语言标题。
def _selected_complex_cases(tmp_path: Path):
    report = _task_suite(tmp_path)
    selected_ids = {
        "github_weekly_star_growth_xlsx",
        "research_documents_translation_pdf",
        "shopping_site_flow",
        "data_analysis_package",
    }
    return [item for item in report.cases if item.case_id in selected_ids]


# LLM: _data_analysis_case_artifacts loads the structured artifacts contract for one case.
# 函数用途: 复用 suite planner 生成的 expected_artifacts，不在测试里复制合同正文。
def _data_analysis_case_artifacts(tmp_path: Path):
    report = _task_suite(tmp_path)
    case = next(item for item in report.cases if item.case_id == "data_analysis_package")
    artifacts = json.loads((tmp_path / case.expected_artifacts_ref).read_text(encoding="utf-8"))
    return case, artifacts


# LLM: _task_suite materializes the refs-first task suite in a temp workspace.
# 函数用途: 生成测试用 prompt/acceptance/expected_artifacts 引用。
def _task_suite(tmp_path: Path):
    from agent_py_agent.agent.contracts.main_agent_task_suite import (
        MainAgentTaskSuiteRequest,
        plan_main_agent_task_suite,
    )

    return plan_main_agent_task_suite(MainAgentTaskSuiteRequest(workspace=tmp_path, max_workers=4))


# LLM: _analysis_rows creates structured rows for collection-count tests.
# 函数用途: 用机器字段生成测试数据，不依赖自然语言报告内容。
def _analysis_rows(count: int) -> list[dict[str, object]]:
    return [
        {
            "订单ID": f"O-{index:04d}",
            "月份": "2026-01",
            "地区": "华东",
            "品类": "家居",
            "销售额": 100 + index,
            "利润": 20,
        }
        for index in range(count)
    ]


# LLM: _generated_analysis_source_call keeps the large-table fixture in structured fields.
# 函数用途: 返回可由 write_structured_json 扩展成 1000 行和 3 个同形 sheet 的通用调用。
def _generated_analysis_source_call() -> dict[str, object]:
    return {
        "path": "outputs/data_analysis/source_data.json",
        "data": {"completion_evidence": {"scope": "synthetic_analysis_dataset", "row_count": 1000}},
        "generated_rows": {
            "count": 1000,
            "columns": ["订单ID", "月份", "地区", "品类", "销售额", "利润"],
            "fields": {
                "订单ID": {"format": "ORD-{index:04d}", "start": 1},
                "月份": {"cycle": ["2026-01", "2026-02", "2026-03", "2026-04"]},
                "地区": {"cycle": ["华东", "华南", "华北", "西南"]},
                "品类": {"cycle": ["电子产品", "服装", "食品", "家居"]},
                "销售额": {"number": {"start": 1000, "step": 73, "modulo": 12000}},
                "利润": {"multiply": {"source": "销售额", "factor": 0.22, "decimals": 2}},
            },
            "sheets": {"count": 3, "prefix": "原始数据"},
        },
    }


# LLM: _write_source_data stores the staged source JSON shape consumed by builders.
# 函数用途: 写入 rows、sheets 和 completion_evidence 三类结构化字段供验收读取。
def _write_source_data(path: Path, rows: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps(
            {
                "completion_evidence": {"scope": "synthetic_analysis_dataset", "row_count": len(rows)},
                "rows": rows,
                "sheets": [{"name": "原始数据", "rows": rows}],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
