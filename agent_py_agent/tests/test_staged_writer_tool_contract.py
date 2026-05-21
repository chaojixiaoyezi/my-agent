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
