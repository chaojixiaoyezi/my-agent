from __future__ import annotations


# LLM: Markdown format checks should use heading/table facts, not report prose scoring.
# 函数用途: 验证 Markdown 缺少声明章节和表格时返回结构化格式错误。
def test_markdown_output_requires_sections_and_tables() -> None:
    from agent_py_agent.agent.contracts.offline_output_format_contract import (
        validate_output_format_contract,
    )

    result = validate_output_format_contract(
        {
            "outputs": [
                {
                    "output_id": "report.md",
                    "kind": "markdown",
                    "text": "# Summary\nok\n",
                    "required_sections": ["Summary", "Evidence"],
                    "required_tables_min": 1,
                    "table_count": 0,
                }
            ]
        }
    )

    assert result.ok is False
    assert result.error_codes == ("MARKDOWN_SECTION_MISSING", "MARKDOWN_TABLE_MISSING")


# LLM: JSON report checks should require declared fields and evidence count.
# 函数用途: 验证 JSON 输出缺少 schema 字段和证据条数时不能通过。
def test_json_output_requires_schema_fields_and_evidence_count() -> None:
    from agent_py_agent.agent.contracts.offline_output_format_contract import (
        validate_output_format_contract,
    )

    result = validate_output_format_contract(
        {
            "outputs": [
                {
                    "output_id": "report.json",
                    "kind": "json",
                    "fields": ["risk_level"],
                    "required_fields": ["risk_level", "evidence", "decision"],
                    "evidence_count": 0,
                    "required_evidence_min": 1,
                }
            ]
        }
    )

    assert result.ok is False
    assert result.error_codes == ("JSON_SCHEMA_FIELD_MISSING", "JSON_EVIDENCE_MISSING")
    assert result.findings[0]["missing_fields"] == ["evidence", "decision"]


# LLM: XLSX output checks should validate sheet names, columns, and row count from workbook facts.
# 函数用途: 验证 xlsx 缺少子表、列和数据行时返回通用格式 finding。
def test_xlsx_output_requires_sheets_columns_and_rows() -> None:
    from agent_py_agent.agent.contracts.offline_output_format_contract import (
        validate_output_format_contract,
    )

    result = validate_output_format_contract(
        {
            "outputs": [
                {
                    "output_id": "report.xlsx",
                    "kind": "xlsx",
                    "sheet_names": ["Summary"],
                    "required_sheets": ["Summary", "Weekly"],
                    "columns": ["项目", "地址"],
                    "required_columns": ["项目", "地址", "说明依据"],
                    "row_count": 0,
                    "required_rows_min": 1,
                }
            ]
        }
    )

    assert result.ok is False
    assert result.error_codes == ("XLSX_SHEET_MISSING", "XLSX_COLUMN_MISSING", "XLSX_ROW_MISSING")


# LLM: Encoding and long-output checks should be generic across artifact kinds.
# 函数用途: 验证中文 UTF-8 解码失败和大输出未外置都会被离线合同拦住。
def test_utf8_and_long_output_contracts_are_generic() -> None:
    from agent_py_agent.agent.contracts.offline_output_format_contract import (
        validate_output_format_contract,
    )

    result = validate_output_format_contract(
        {
            "outputs": [
                {"output_id": "cn.md", "kind": "markdown", "encoding": "utf-8", "encoding_valid": False},
                {
                    "output_id": "large.txt",
                    "kind": "text",
                    "inline_bytes": 4096,
                    "inline_budget_bytes": 512,
                    "artifact_refs": [],
                    "truncated": False,
                },
            ]
        }
    )

    assert result.ok is False
    assert result.error_codes == ("UTF8_ENCODING_INVALID", "LONG_OUTPUT_NOT_EXTERNALIZED")
