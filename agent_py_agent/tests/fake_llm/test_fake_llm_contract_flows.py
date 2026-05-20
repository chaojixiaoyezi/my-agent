from __future__ import annotations

import json
from pathlib import Path


def test_fake_llm_claims_success_without_artifact_is_rejected(tmp_path: Path):
    from agent_py_agent.tests.support.fake_llm_runner import FakeLLMRunner

    runner = FakeLLMRunner.from_fixture(_fixture("claim_success_without_artifact.json"))

    result = runner.run(tmp_path)

    assert result.final_status == "SUCCEEDED"
    assert result.contract_result.ok is False
    assert "ARTIFACT_MISSING" in result.contract_result.error_codes
    assert "FINAL_STATUS_REJECTED" in result.contract_result.error_codes


def test_fake_llm_writes_empty_artifact_is_rejected(tmp_path: Path):
    from agent_py_agent.tests.support.fake_llm_runner import FakeLLMRunner

    runner = FakeLLMRunner.from_fixture(_fixture("write_empty_artifact.json"))

    result = runner.run(tmp_path)

    assert (tmp_path / "output.md").exists()
    assert result.contract_result.ok is False
    assert "ARTIFACT_TOO_SMALL" in result.contract_result.error_codes


def test_fake_llm_claims_success_with_empty_structured_checkpoint_is_rejected(tmp_path: Path):
    from agent_py_agent.tests.support.fake_llm_runner import FakeLLMRunner

    runner = FakeLLMRunner.from_fixture(_fixture("staged_json_no_rows_then_success.json"))

    result = runner.run(tmp_path)

    assert result.contract_result.ok is False
    assert "REQUIRED_JSON_COLLECTION_EMPTY" in result.contract_result.error_codes
    assert "FINAL_STATUS_REJECTED" in result.contract_result.error_codes


def test_fake_llm_claims_success_without_builder_call_is_rejected(tmp_path: Path):
    from agent_py_agent.tests.support.fake_llm_runner import FakeLLMRunner

    runner = FakeLLMRunner.from_fixture(_fixture("builder_not_called_then_success.json"))

    result = runner.run(tmp_path)

    assert result.contract_result.ok is False
    assert "REQUIRED_SUCCESSFUL_TOOL_CALL_MISSING" in result.contract_result.error_codes
    assert "FINAL_STATUS_REJECTED" in result.contract_result.error_codes


def test_fake_llm_repeated_missing_tool_call_is_blocked(tmp_path: Path):
    from agent_py_agent.tests.support.fake_llm_runner import FakeLLMRunner

    runner = FakeLLMRunner.from_fixture(_fixture("repeated_missing_read.json"))

    result = runner.run(tmp_path)

    assert result.blocked is True
    assert result.block_reason == "TOOL_REPEATED_EXACT_FAILURE"


def test_fake_llm_unknown_tool_and_dangerous_action_are_rejected(tmp_path: Path):
    from agent_py_agent.tests.support.fake_llm_runner import FakeLLMRunner

    runner = FakeLLMRunner.from_fixture(_fixture("unknown_and_dangerous_tools.json"))

    result = runner.run(tmp_path)

    error_codes = [item["result"].get("error_code") for item in result.tool_trace]
    assert "TOOL_NOT_FOUND" in error_codes
    assert "APPROVAL_REQUIRED" in error_codes
    assert result.contract_result.ok is False


def test_fake_llm_missing_final_report_is_rejected(tmp_path: Path):
    from agent_py_agent.tests.support.fake_llm_runner import FakeLLMRunner

    runner = FakeLLMRunner.from_fixture(_fixture("missing_final_report.json"))

    result = runner.run(tmp_path)

    assert result.final_status == "UNKNOWN"
    assert result.contract_result.ok is False
    assert "FINAL_STATUS_MISSING" in result.contract_result.error_codes


def _fixture(name: str) -> dict[str, object]:
    path = Path(__file__).parent / name
    return json.loads(path.read_text(encoding="utf-8"))
