from __future__ import annotations

import json
from pathlib import Path


def test_failed_write_tool_cannot_be_counted_as_successful_artifact_work(tmp_path: Path):
    from agent_py_agent.tests.support.contract_fixture_runner import verify_contract_fixture

    contract = _fixture("missing_artifact_should_fail.json")
    tool_trace = [
        {
            "tool": "write_file",
            "params": {"path": "output.md"},
            "result": {"ok": False, "error_code": "PATH_PERMISSION_DENIED"},
        }
    ]

    result = verify_contract_fixture(tmp_path, contract, tool_trace=tool_trace, final_status="SUCCEEDED")

    assert result.ok is False
    assert "ARTIFACT_MISSING" in result.error_codes
    assert "FINAL_STATUS_REJECTED" in result.error_codes


def _fixture(name: str) -> dict[str, object]:
    path = Path(__file__).parents[1] / "contracts" / name
    return json.loads(path.read_text(encoding="utf-8"))
