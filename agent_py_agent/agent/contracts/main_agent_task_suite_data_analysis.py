# LLM: Data-analysis task contracts stay outside the suite index to keep gates small.
# 模块用途: 定义通用数据分析交付包，覆盖 source JSON、xlsx、PDF 和 HTML 多产物验收。

from __future__ import annotations

from .main_agent_task_suite import MainAgentTaskArtifact, MainAgentTaskCase


# LLM: data_analysis_package_case covers structured data, workbook, report PDF, and dashboard.
# 函数用途: 定义通用数据分析交付包；验收靠产物类型和结构化数量字段，不写专项业务判断。
def data_analysis_package_case() -> MainAgentTaskCase:
    artifacts = (
        _source_data_artifact(),
        _workbook_artifact(),
        _pdf_report_artifact(),
        _html_dashboard_artifact(),
    )
    return MainAgentTaskCase(
        case_id="data_analysis_package",
        title="数据分析工作簿、PDF 和 HTML 仪表盘",
        user_prompt=(
            "生成一份模拟业务数据，至少1000行。然后做成一个xlsx分析表、一个PDF分析报告、"
            "一个能直接打开看的HTML仪表盘。要包含销售额、利润、地区、品类、月份趋势、异常点和中文建议，"
            "每个产物都要完整可打开。"
        ),
        artifacts=artifacts,
        acceptance_checks=tuple(
            _artifact_check(
                f"{artifact.artifact_id}_accepted",
                "artifact_acceptance",
                artifact.artifact_id,
            )
            for artifact in artifacts
        ),
    )


# LLM: _source_data_artifact anchors large analysis tasks in structured rows before reports.
# 函数用途: 要求先落 source_data.json，后续 xlsx/pdf/html 都从同一结构化数据事实出发。
def _source_data_artifact() -> MainAgentTaskArtifact:
    return MainAgentTaskArtifact(
        artifact_id="analysis_source_data",
        kind="json",
        preferred_path="outputs/data_analysis/source_data.json",
        validation_contract={
            "validator": "artifact_acceptance",
            "required_fields": ["rows"],
        },
    )


# LLM: _workbook_artifact requires the generic workbook builder and tabular source coverage.
# 函数用途: 通过 collection_contract 与 staging_contract 拒绝少量手写行或空壳 xlsx。
def _workbook_artifact() -> MainAgentTaskArtifact:
    return MainAgentTaskArtifact(
        artifact_id="analysis_workbook",
        kind="xlsx",
        preferred_path="outputs/data_analysis/analysis.xlsx",
        validation_contract={
            "validator": "spreadsheet_acceptance",
            "required_sheets_min": 3,
            "required_columns": _required_columns(),
            "collection_contract": _collection_contract(),
            "staging_contract": _workbook_staging_contract(),
        },
    )


# LLM: _workbook_staging_contract describes the source-data to workbook build path.
# 函数用途: 固化数据分析包的 source JSON、builder tool、workbook ref 和 checkpoint 提示。
def _workbook_staging_contract() -> dict[str, object]:
    return {
        "strategy": "data_then_tool_builder_then_workbook",
        "builder_tool": "data_to_workbook",
        "source_json_ref": "outputs/data_analysis/source_data.json",
        "workbook_ref": "outputs/data_analysis/analysis.xlsx",
        "checkpoint_shape_hints": {
            "outputs/data_analysis/source_data.json": (
                '{"data":{"completion_evidence":{"scope":"synthetic_analysis_dataset","row_count":1000}},'
                '"generated_rows":{"count":1000,"columns":["订单ID","月份","地区","品类","销售额","利润"],'
                '"fields":{"订单ID":{"format":"ORD-{index:04d}","start":1},'
                '"月份":{"cycle":["2026-01","2026-02"]},'
                '"地区":{"cycle":["华东","华南"]},"品类":{"cycle":["电子产品","服装"]},'
                '"销售额":{"number":{"start":1000,"step":73,"modulo":12000}},'
                '"利润":{"multiply":{"source":"销售额","factor":0.22,"decimals":2}}},'
                '"sheets":{"count":3,"prefix":"原始数据"}}}'
            )
        },
        "checkpoint_refs": [
            "outputs/data_analysis/source_data.json",
            "outputs/data_analysis/analysis.xlsx",
        ],
    }


# LLM: _pdf_report_artifact keeps PDF creation on the generic markdown_to_pdf path.
# 函数用途: 要求先生成 Markdown 草稿再构建 PDF，避免模型只声称“报告完成”。
def _pdf_report_artifact() -> MainAgentTaskArtifact:
    return MainAgentTaskArtifact(
        artifact_id="analysis_pdf_report",
        kind="pdf",
        preferred_path="outputs/data_analysis/report.pdf",
        validation_contract={
            "validator": "document_acceptance",
            "required_suffix": ".pdf",
            "collection_contract": _collection_contract(),
            "staging_contract": {
                "strategy": "source_data_then_markdown_report_then_pdf",
                "builder_tool": "markdown_to_pdf",
                "source_json_ref": "outputs/data_analysis/source_data.json",
                "source_markdown_ref": "outputs/data_analysis/report.md",
                "pdf_ref": "outputs/data_analysis/report.pdf",
                "checkpoint_refs": [
                    "outputs/data_analysis/source_data.json",
                    "outputs/data_analysis/report.md",
                    "outputs/data_analysis/report.pdf",
                ],
            },
        },
    )


# LLM: _html_dashboard_artifact verifies the dashboard as a normal HTML artifact.
# 函数用途: HTML 仪表盘只走通用 HTML 完整性、大小和本地资源检查。
def _html_dashboard_artifact() -> MainAgentTaskArtifact:
    return MainAgentTaskArtifact(
        artifact_id="analysis_html_dashboard",
        kind="html",
        preferred_path="outputs/data_analysis/dashboard.html",
        validation_contract={
            "validator": "artifact_acceptance",
            "required_suffix": ".html",
            "quality_requirements": {
                "complete_html_document": True,
                "images_must_be_local_or_inline": True,
                "min_size_bytes": 3000,
                "single_file_no_external_assets": True,
            },
        },
    )


# LLM: _collection_contract is a reusable table-size contract for analysis packages.
# 函数用途: 用结构化 row count 和必填列约束复杂数据任务，不依赖报告正文自称有多少行。
def _collection_contract() -> dict[str, object]:
    return {
        "source_json_ref": "outputs/data_analysis/source_data.json",
        "items_path": "rows",
        "min_items_total": 1000,
        "required_item_fields": _required_columns(),
        "require_completion_evidence": True,
        "completion_evidence_path": "completion_evidence",
    }


# LLM: _required_columns centralizes table fields used by JSON and XLSX contracts.
# 函数用途: 保持数据源和工作簿的字段一致，避免测试和合同出现两套列名。
def _required_columns() -> list[str]:
    return ["订单ID", "月份", "地区", "品类", "销售额", "利润"]


# LLM: _artifact_check keeps acceptance contracts structured and reusable.
# 函数用途: 生成通用 artifact 验收项，避免在代码里解析任务 prompt 的自然语言。
def _artifact_check(check_id: str, kind: str, artifact_id: str) -> dict[str, object]:
    return {"check_id": check_id, "kind": kind, "artifact_id": artifact_id}


__all__ = ["data_analysis_package_case"]
