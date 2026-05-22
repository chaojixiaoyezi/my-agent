from __future__ import annotations

from pathlib import Path


# LLM: Staged writer contracts are machine facts, not prompt hints.
# 函数用途: 验证结构化 JSON checkpoint 声明后，普通写文件工具不能绕过 write_structured_json 校验。
def test_staged_writer_contract_blocks_plain_file_write_to_json_checkpoint(tmp_path: Path):
    from agent_py_agent.agent.agent_core.tool_staged_writer_contract import (
        staged_writer_contract_result,
    )

    result = staged_writer_contract_result(
        _params(_xlsx_delivery_contract()),
        {"tool": "write_file", "path": "outputs/report/source_data.json", "content": "{}"},
    )

    assert result is not None
    assert result.ok is False
    assert result.tool == "write_file"
    assert "STAGED_WRITER_TOOL_MISMATCH" in result.output


# LLM: The declared writer tool remains the allowed path for staged checkpoints.
# 函数用途: 验证同一 checkpoint 用合同指定的 write_structured_json 可以继续执行。
def test_staged_writer_contract_allows_declared_structured_json_writer():
    from agent_py_agent.agent.agent_core.tool_staged_writer_contract import (
        staged_writer_contract_result,
    )

    result = staged_writer_contract_result(
        _params(_xlsx_delivery_contract()),
        {
            "tool": "write_structured_json",
            "path": "outputs/report/source_data.json",
            "rows": [{"project": "demo"}],
        },
    )

    assert result is None


# LLM: Builder outputs must be produced by their structured builder, not by arbitrary file writes.
# 函数用途: 验证 xlsx 输出声明 builder_tool 后，不能被 write_file 直接伪造。
def test_staged_writer_contract_blocks_plain_file_write_to_builder_output():
    from agent_py_agent.agent.agent_core.tool_staged_writer_contract import (
        staged_writer_contract_result,
    )

    result = staged_writer_contract_result(
        _params(_xlsx_delivery_contract()),
        {"tool": "write_file", "path": "outputs/report/report.xlsx", "content": "fake"},
    )

    assert result is not None
    assert result.ok is False
    assert result.tool == "write_file"
    assert "required_tool=data_to_workbook" in result.output


# LLM: Builder tools are the valid write channel for staged artifact outputs.
# 函数用途: 验证 data_to_workbook 写 workbook_ref 时不会被 staged writer guard 误拦。
def test_staged_writer_contract_allows_declared_builder_output_tool():
    from agent_py_agent.agent.agent_core.tool_staged_writer_contract import (
        staged_writer_contract_result,
    )

    result = staged_writer_contract_result(
        _params(_xlsx_delivery_contract()),
        {
            "tool": "data_to_workbook",
            "source_json_path": "outputs/report/source_data.json",
            "path": "outputs/report/report.xlsx",
        },
    )

    assert result is None


def test_api_collection_contract_blocks_missing_required_field_mappings():
    from agent_py_agent.agent.agent_core.tool_api_collection_contract import (
        api_collection_contract_result,
    )

    result = api_collection_contract_result(
        _params(_xlsx_delivery_contract()),
        {
            "fields": {"项目名": "full_name", "地址": "html_url"},
            "path": "outputs/report/source_data.json",
            "request_ranges": [{"end_date": "2026-05-22", "start_date": "2026-01-01", "step_days": 7}],
            "tool": "api_json_collection",
        },
    )

    assert result is not None
    assert result.ok is False
    assert "missing field mappings" in result.output
    assert "template/default_template" in result.output


def test_api_collection_contract_blocks_too_few_planned_groups():
    from agent_py_agent.agent.agent_core.tool_api_collection_contract import (
        api_collection_contract_result,
    )

    result = api_collection_contract_result(
        _params(_xlsx_delivery_contract()),
        {
            "fields": {"项目名": "full_name", "地址": "html_url", "上升 star 数": "stargazers_count"},
            "path": "outputs/report/source_data.json",
            "request_ranges": [{"end_date": "2026-01-14", "start_date": "2026-01-01", "step_days": 7}],
            "tool": "api_json_collection",
        },
    )

    assert result is not None
    assert result.ok is False
    assert "planned groups 2 below required 20" in result.output


def test_structured_json_source_checkpoint_blocks_missing_row_evidence():
    from agent_py_agent.agent.agent_core.tool_api_collection_contract import (
        api_collection_contract_result,
    )

    result = api_collection_contract_result(
        _params(_sourced_xlsx_delivery_contract()),
        {
            "path": "outputs/report/source_data.json",
            "rows": [{"项目名": "org/demo", "地址": "https://github.com/org/demo"}],
            "tool": "write_structured_json",
        },
    )

    assert result is not None
    assert result.ok is False
    assert "source checkpoint requires auditable source_refs and claims" in result.output


def test_structured_json_source_checkpoint_blocks_unbound_source_refs():
    from agent_py_agent.agent.agent_core.tool_api_collection_contract import (
        api_collection_contract_result,
    )

    result = api_collection_contract_result(
        _params(_sourced_xlsx_delivery_contract()),
        {
            "claims": [{"field": "项目名", "source_ids": ["src-1"], "value": "org/demo"}],
            "completion_evidence": {"scope": "fixture"},
            "path": "outputs/report/source_data.json",
            "source_refs": [{"content_sha256": "abc", "source_id": "src-1", "uri": "https://example.test/data.json"}],
            "tool": "write_structured_json",
        },
    )

    assert result is not None
    assert result.ok is False
    assert "artifact_ref or reserved.tool_call_id" in result.output


def test_structured_json_source_checkpoint_blocks_unrecorded_tool_call_binding():
    from agent_py_agent.agent.agent_core.tool_api_collection_contract import (
        api_collection_contract_result,
    )

    result = api_collection_contract_result(
        _params(_sourced_xlsx_delivery_contract()),
        {
            "claims": [{"field": "项目名", "source_ids": ["src-1"], "value": "org/demo"}],
            "completion_evidence": {"scope": "fixture"},
            "path": "outputs/report/source_data.json",
            "source_refs": [
                {
                    "reserved": {"tool_call_id": "missing-call"},
                    "source_id": "src-1",
                    "uri": "https://example.test/data.json",
                }
            ],
            "tool": "write_structured_json",
        },
    )

    assert result is not None
    assert result.ok is False
    assert "reserved.tool_call_id must match previous archive_tool_calls" in result.output


def test_structured_json_source_checkpoint_blocks_missing_required_claim_fields():
    from agent_py_agent.agent.agent_core.tool_api_collection_contract import (
        api_collection_contract_result,
    )

    result = api_collection_contract_result(
        _params(_sourced_xlsx_delivery_contract()),
        {
            "claims": [{"field": "项目名", "source_ids": ["src-1"], "value": "org/demo"}],
            "completion_evidence": {"scope": "fixture"},
            "path": "outputs/report/source_data.json",
            "source_refs": [{"artifact_ref": "artifacts/raw.json", "source_id": "src-1"}],
            "tool": "write_structured_json",
        },
    )

    assert result is not None
    assert result.ok is False
    assert "missing required claim fields" in result.output
    assert "上升 star 数" in result.output
    assert "地址" in result.output


def test_structured_json_source_checkpoint_blocks_unverified_claims_when_required():
    from agent_py_agent.agent.agent_core.tool_api_collection_contract import (
        api_collection_contract_result,
    )

    result = api_collection_contract_result(
        _params(_sourced_xlsx_delivery_contract()),
        {
            "claims": [
                {"field": "项目名", "source_ids": ["src-1"], "value": "org/demo", "verification_status": "PENDING"},
                {"field": "地址", "source_ids": ["src-1"], "value": "https://github.com/org/demo"},
                {"field": "上升 star 数", "source_ids": ["src-1"], "value": 100},
            ],
            "completion_evidence": {"scope": "fixture"},
            "path": "outputs/report/source_data.json",
            "source_refs": [{"artifact_ref": "artifacts/raw.json", "source_id": "src-1"}],
            "tool": "write_structured_json",
        },
    )

    assert result is not None
    assert result.ok is False
    assert "claims must be VERIFIED" in result.output


# LLM: JSON checkpoint evidence rules apply even when the builder source is a markdown file.
# 函数用途: 验证 collection_contract.source_json_ref 也受来源绑定门保护，不只保护 staging.source_json_ref。
def test_structured_json_collection_checkpoint_blocks_unrecorded_binding_without_source_json_ref():
    from agent_py_agent.agent.agent_core.tool_api_collection_contract import (
        api_collection_contract_result,
    )

    result = api_collection_contract_result(
        _params(_sourced_document_delivery_contract()),
        {
            "claims": [{"field": "title", "source_ids": ["src-1"], "value": "demo"}],
            "completion_evidence": {"scope": "fixture"},
            "path": "outputs/docs/source_index.json",
            "source_refs": [
                {
                    "reserved": {"tool_call_id": "placeholder"},
                    "source_id": "src-1",
                    "uri": "https://example.test/paper",
                }
            ],
            "tool": "write_structured_json",
        },
    )

    assert result is not None
    assert result.ok is False
    assert "reserved.tool_call_id must match previous archive_tool_calls" in result.output


def test_structured_json_source_checkpoint_allows_recorded_tool_call_binding():
    from agent_py_agent.agent.agent_core.tool_api_collection_contract import (
        api_collection_contract_result,
    )

    params = _params(_sourced_xlsx_delivery_contract())
    params.archive_tool_calls.append({"call_id": "1-1", "ok": True, "tool": "fetch_url"})
    result = api_collection_contract_result(
        params,
        {
            "claims": _project_claims("src-1"),
            "completion_evidence": {"scope": "fixture"},
            "path": "outputs/report/source_data.json",
            "source_refs": [
                {
                    "reserved": {"tool_call_id": "1-1"},
                    "source_id": "src-1",
                    "uri": "https://example.test/data.json",
                }
            ],
            "tool": "write_structured_json",
        },
    )

    assert result is None


def test_structured_json_source_checkpoint_blocks_missing_completion_evidence():
    from agent_py_agent.agent.agent_core.tool_api_collection_contract import (
        api_collection_contract_result,
    )

    result = api_collection_contract_result(
        _params(_sourced_xlsx_delivery_contract()),
        {
            "claims": [{"field": "项目名", "source_ids": ["src-1"], "value": "org/demo"}],
            "path": "outputs/report/source_data.json",
            "source_refs": [{"artifact_ref": "artifacts/raw.json", "source_id": "src-1"}],
            "tool": "write_structured_json",
        },
    )

    assert result is not None
    assert result.ok is False
    assert "completion_evidence" in result.output


def test_structured_json_source_checkpoint_allows_bound_source_evidence():
    from agent_py_agent.agent.agent_core.tool_api_collection_contract import (
        api_collection_contract_result,
    )

    result = api_collection_contract_result(
        _params(_sourced_xlsx_delivery_contract()),
        {
            "claims": _project_claims("src-1"),
            "completion_evidence": {"scope": "fixture"},
            "path": "outputs/report/source_data.json",
            "source_refs": [{"artifact_ref": "artifacts/raw.json", "source_id": "src-1"}],
            "tool": "write_structured_json",
        },
    )

    assert result is None


# LLM: Subagent default tools must mirror main-agent artifact builders for staged deliverables.
# 函数用途: 验证子代理基础工具包包含结构化 checkpoint、workbook 和 PDF 构建工具。
def test_subagent_default_tool_grants_include_structured_artifact_builders():
    from agent_py_agent.agent.agent_core.orchestration_tool_grants import CODING_SUBAGENT_TOOLS
    from agent_py_agent.agent.subagents.role_templates import ROLE_BASE_TOOLS

    expected = {"file_write_session", "write_structured_json", "data_to_workbook", "markdown_to_pdf"}

    assert expected.issubset(set(CODING_SUBAGENT_TOOLS))
    assert expected.issubset(set(ROLE_BASE_TOOLS))


def _params(delivery_contract: dict[str, object]):
    from agent_py_agent.agent.agent_core._runtime_params import ToolLoopExecuteParams

    return ToolLoopExecuteParams(
        user_prompt="test",
        memories=[],
        runtime_injections=[],
        prompt_files=[],
        tool_catalog_section="",
        tool_recommendations_section="",
        tool_context=[],
        effective_on_chunk=None,
        allowed_tools=None,
        granted_capabilities=None,
        write_boundary=None,
        task_attributes={},
        request_id="req-1",
        run_id="run-1",
        task_id="task-1",
        one_shot_tool_calls=set(),
        executed_tools=[],
        archive_tool_calls=[],
        delivery_contract=delivery_contract,
    )


def _xlsx_delivery_contract() -> dict[str, object]:
    return {
        "artifacts": [
            {
                "artifact_id": "report",
                "preferred_path": "outputs/report/report.xlsx",
                "validation_contract": {
                    "collection_contract": {
                        "groups_path": "sheets",
                        "items_path": "rows",
                        "min_groups": 20,
                        "required_item_fields": ["项目名", "地址", "上升 star 数"],
                    },
                    "required_columns": ["项目名", "地址", "上升 star 数"],
                    "staging_contract": {
                        "builder_tool": "data_to_workbook",
                        "source_json_ref": "outputs/report/source_data.json",
                        "workbook_ref": "outputs/report/report.xlsx",
                        "checkpoint_refs": [
                            "outputs/report/source_data.json",
                            "outputs/report/report.xlsx",
                        ],
                        "checkpoint_shape_hints": {
                            "outputs/report/source_data.json": '{"sheets":[{"rows":[{"project":"..."}]}]}',
                        },
                    }
                },
            }
        ],
        "bootstrap_contract": {
            "startup_actions": [
                {
                    "action": "materialize_checkpoint",
                    "checkpoint_ref": "outputs/report/source_data.json",
                },
                {
                    "action": "invoke_builder_tool",
                    "builder_tool": "data_to_workbook",
                    "source_ref": "outputs/report/source_data.json",
                    "output_ref": "outputs/report/report.xlsx",
                },
            ]
        },
    }


def _sourced_xlsx_delivery_contract() -> dict[str, object]:
    contract = _xlsx_delivery_contract()
    validation = contract["artifacts"][0]["validation_contract"]
    validation["evidence_contract"] = {
        "require_verified": True,
        "required_fields": ["项目名", "地址", "上升 star 数"],
    }
    validation["collection_contract"] = {
        **validation["collection_contract"],
        "require_completion_evidence": True,
        "require_item_evidence": True,
        "required_item_evidence_fields": ["项目名", "地址"],
    }
    return contract


def _project_claims(source_id: str) -> list[dict[str, object]]:
    return [
        {"field": "项目名", "source_ids": [source_id], "value": "org/demo"},
        {"field": "地址", "source_ids": [source_id], "value": "https://github.com/org/demo"},
        {"field": "上升 star 数", "source_ids": [source_id], "value": 100},
    ]


def _sourced_document_delivery_contract() -> dict[str, object]:
    return {
        "artifacts": [
            {
                "artifact_id": "document_pdf",
                "preferred_path": "outputs/docs/report.pdf",
                "validation_contract": {
                    "collection_contract": {
                        "source_json_ref": "outputs/docs/source_index.json",
                        "require_completion_evidence": True,
                        "require_item_evidence": True,
                        "required_item_evidence_fields": ["title", "url", "date"],
                    },
                    "evidence_contract": {
                        "require_verified": True,
                        "required_fields": ["title", "url", "date"],
                    },
                    "staging_contract": {
                        "builder_tool": "markdown_to_pdf",
                        "source_markdown_ref": "outputs/docs/report.md",
                        "pdf_ref": "outputs/docs/report.pdf",
                        "checkpoint_refs": [
                            "outputs/docs/source_index.json",
                            "outputs/docs/report.md",
                            "outputs/docs/report.pdf",
                        ],
                    },
                },
            }
        ]
    }
