from __future__ import annotations

import json
from pathlib import Path


def test_contract_fixture_rejects_missing_artifact(tmp_path: Path):
    from agent_py_agent.tests.support.contract_fixture_runner import verify_contract_fixture

    contract = _fixture("missing_artifact_should_fail.json")

    result = verify_contract_fixture(tmp_path, contract, tool_trace=[{"tool": "write_file"}], final_status="SUCCEEDED")

    assert result.ok is False
    assert "ARTIFACT_MISSING" in result.error_codes
    assert "FINAL_STATUS_REJECTED" in result.error_codes


def test_contract_fixture_rejects_empty_artifact(tmp_path: Path):
    from agent_py_agent.tests.support.contract_fixture_runner import verify_contract_fixture

    contract = _fixture("empty_artifact_should_fail.json")
    (tmp_path / "output.md").write_text("", encoding="utf-8")

    result = verify_contract_fixture(tmp_path, contract, tool_trace=[{"tool": "write_file"}], final_status="SUCCEEDED")

    assert result.ok is False
    assert "ARTIFACT_TOO_SMALL" in result.error_codes
    assert "FINAL_STATUS_REJECTED" in result.error_codes


def test_contract_fixture_rejects_missing_tool_trace(tmp_path: Path):
    from agent_py_agent.tests.support.contract_fixture_runner import verify_contract_fixture

    contract = _fixture("simple_file_summary.json")
    (tmp_path / "output.md").write_text("Summary\nEnough content\nChecked Files\ninput.txt\n", encoding="utf-8")

    result = verify_contract_fixture(tmp_path, contract, tool_trace=[], final_status="SUCCEEDED")

    assert result.ok is False
    assert "TOOL_TRACE_EMPTY" in result.error_codes
    assert "REQUIRED_TOOL_CALL_MISSING" in result.error_codes


def _fixture(name: str) -> dict[str, object]:
    path = Path(__file__).parent / name
    return json.loads(path.read_text(encoding="utf-8"))
