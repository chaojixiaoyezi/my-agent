from __future__ import annotations

import json
from pathlib import Path


def test_failed_write_tool_cannot_be_counted_as_successful_artifact_work(tmp_path: Path):
    from agent_py_agent.tests.support.contract_fixture_runner import (
        FixtureRunFacts,
        verify_contract_fixture,
    )

    contract = _fixture("missing_artifact_should_fail.json")
    tool_trace = [
        {
            "tool": "write_file",
            "params": {"path": "output.md"},
            "result": {"ok": False, "error_code": "PATH_PERMISSION_DENIED"},
        }
    ]

    result = verify_contract_fixture(
        tmp_path,
        contract,
        FixtureRunFacts(tool_trace=tuple(tool_trace), final_status="SUCCEEDED"),
    )

    assert result.ok is False
    assert "ARTIFACT_MISSING" in result.error_codes
    assert "FINAL_STATUS_REJECTED" in result.error_codes


def test_fake_tool_runner_covers_fetch_workbook_dangerous_and_timeout(tmp_path: Path):
    from agent_py_agent.tests.support.fake_tools import FakeToolRunner

    runner = FakeToolRunner(
        tmp_path,
        fixtures={
            "fetch_url": {
                "https://example.com/data.json": {"ok": True, "body": '{"rows":[{"项目":"demo"}]}'},
                "https://example.com/timeout": {"ok": False, "error_code": "TOOL_TIMEOUT"},
            }
        },
    )
    source = tmp_path / "source.json"
    source.write_text('{"sheets":[{"name":"Sheet1","rows":[{"项目":"demo"}]}]}', encoding="utf-8")

    ok_fetch = runner.execute("fetch_url", {"url": "https://example.com/data.json"})
    timeout_fetch = runner.execute("fetch_url", {"url": "https://example.com/timeout"})
    workbook = runner.execute("data_to_workbook", {"source_json_path": "source.json", "path": "report.xlsx"})
    dangerous = runner.execute("dangerous_command", {"command": "rm -rf /"})

    assert ok_fetch["ok"] is True
    assert timeout_fetch["error_code"] == "TOOL_TIMEOUT"
    assert workbook["ok"] is True
    assert (tmp_path / "report.xlsx").exists()
    assert dangerous["ok"] is False
    assert dangerous["error_code"] == "APPROVAL_REQUIRED"


def test_fake_tool_runner_can_inject_write_failure_from_fixture(tmp_path: Path):
    from agent_py_agent.tests.support.fake_tools import FakeToolRunner

    runner = FakeToolRunner(
        tmp_path,
        fixtures={"write_file": {"output.md": {"ok": False, "error_code": "PATH_PERMISSION_DENIED"}}},
    )

    result = runner.execute("write_file", {"path": "output.md", "content": "demo"})

    assert result["ok"] is False
    assert result["error_code"] == "PATH_PERMISSION_DENIED"
    assert not (tmp_path / "output.md").exists()


def _fixture(name: str) -> dict[str, object]:
    path = Path(__file__).parents[1] / "contracts" / name
    return json.loads(path.read_text(encoding="utf-8"))
