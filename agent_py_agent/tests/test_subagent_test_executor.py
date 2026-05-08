"""测试子代理真实验收执行器。"""

from pathlib import Path

from agent_py_agent.agent.subagents.models import TestExecutor


def test_test_executor_runs_allowed_command_and_records_exit_code(tmp_path):
    executor = TestExecutor(tmp_path, timeout_seconds=10)

    record = executor.execute({
        "name": "python smoke",
        "validation_method": "command",
        "command": "python -c \"print('ok')\"",
    })

    assert record.executed is True
    assert record.exit_code == 0
    assert record.passed is True
    assert record.validation_method == "command"
    assert record.validation_result["ok"] is True
    assert "ok" in record.stdout
    assert record.duration_seconds >= 0
    assert record.executed_at


def test_test_executor_blocks_high_risk_shell_characters(tmp_path):
    executor = TestExecutor(tmp_path)

    record = executor.execute({
        "name": "unsafe",
        "validation_method": "command",
        "command": "python -c \"print('ok')\"; echo unsafe",
    })

    assert record.executed is False
    assert record.passed is False
    assert record.validation_method == "command"
    assert "高风险 shell 字符" in record.error


def test_test_executor_file_check_records_existing_file_metadata(tmp_path):
    target = tmp_path / "result.json"
    target.write_text("{\"status\": \"success\"}", encoding="utf-8")
    executor = TestExecutor(tmp_path)

    record = executor.execute({
        "name": "result exists",
        "validation_method": "file_check",
        "file_path": "result.json",
    })

    assert record.executed is True
    assert record.passed is True
    assert record.command == ""
    assert record.validation_result["ok"] is True
    assert record.validation_result["exists"] is True
    assert record.validation_result["size"] == target.stat().st_size
    assert Path(record.validation_result["path"]).name == "result.json"


def test_test_executor_content_check_matches_literal_pattern(tmp_path):
    (tmp_path / "report.txt").write_text("alpha\nstatus: success\n", encoding="utf-8")
    executor = TestExecutor(tmp_path)

    record = executor.execute({
        "name": "report contains success",
        "validation_method": "content_check",
        "file_path": "report.txt",
        "content_pattern": "status: success",
    })

    assert record.executed is True
    assert record.passed is True
    assert record.validation_method == "content_check"
    assert record.validation_result["ok"] is True
    assert record.validation_result["matched"] is True
