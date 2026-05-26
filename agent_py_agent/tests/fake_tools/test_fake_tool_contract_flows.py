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


def test_fake_tool_runner_covers_fetch_workbook_fixture_dangerous_and_timeout(tmp_path: Path):
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
    workbook = runner.execute("write_workbook_fixture", {"source_json_path": "source.json", "path": "report.xlsx"})
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


def test_fake_file_tool_rejects_write_path_outside_run_dir(tmp_path: Path):
    from agent_py_agent.tests.support.fake_tools import FakeToolRunner

    runner = FakeToolRunner(tmp_path)

    result = runner.execute("write_file", {"path": "../escape.md", "content": "bad"})

    assert result["ok"] is False
    assert result["error_code"] == "PATH_OUTSIDE_RUN_DIR"
    assert not (tmp_path.parent / "escape.md").exists()


def test_fake_file_tool_rejects_read_path_outside_run_dir(tmp_path: Path):
    from agent_py_agent.tests.support.fake_tools import FakeToolRunner

    outside = tmp_path.parent / "secret.txt"
    outside.write_text("secret", encoding="utf-8")
    runner = FakeToolRunner(tmp_path)

    result = runner.execute("read_file", {"path": "../secret.txt"})

    assert result["ok"] is False
    assert result["error_code"] == "PATH_OUTSIDE_RUN_DIR"
    assert result.get("content") is None


def test_fake_tool_runner_wraps_invalid_tool_result_as_structured_error(tmp_path: Path):
    from agent_py_agent.tests.support.fake_tools import FakeToolRunner

    runner = FakeToolRunner(tmp_path, fixtures={"read_file": {"broken.txt": None}})

    result = runner.execute("read_file", {"path": "broken.txt"})

    assert result["ok"] is False
    assert result["error_code"] == "TOOL_RESULT_INVALID"
    assert runner.trace[-1]["result"]["error_code"] == "TOOL_RESULT_INVALID"


def test_fake_tool_runner_applies_structured_tool_policy_before_execution(tmp_path: Path):
    from agent_py_agent.agent.contracts.tool_call_policy import ToolCallPolicy
    from agent_py_agent.tests.support.fake_tools import FakeToolRunner

    runner = FakeToolRunner(
        tmp_path,
        policy=ToolCallPolicy(
            available_tools=("read_file", "write_file"),
            allowed_tools=("read_file",),
            required_parameters={"read_file": ("path",)},
        ),
    )

    missing_param = runner.execute("read_file", {})
    not_allowed = runner.execute("write_file", {"path": "out.md", "content": "demo"})

    assert missing_param["ok"] is False
    assert missing_param["error_code"] == "TOOL_PARAMETER_REQUIRED"
    assert not_allowed["ok"] is False
    assert not_allowed["error_code"] == "TOOL_NOT_ALLOWED"
    assert not (tmp_path / "out.md").exists()


def _fixture(name: str) -> dict[str, object]:
    path = Path(__file__).parents[1] / "contracts" / name
    return json.loads(path.read_text(encoding="utf-8"))
