"""测试子代理真实验收执行报告落盘。"""

import json

from agent_py_agent.agent.subagents.execution_report import (
    TestExecutionReportOptions,
    load_test_execution_report,
    write_test_execution_report,
)
from agent_py_agent.agent.subagents.models import TestExecutionRecord


def _mixed_records():
    return [
        TestExecutionRecord(
            test_name="unit pass",
            command="python -m pytest -q",
            executed=True,
            exit_code=0,
            validation_method="command",
            validation_result={"ok": True},
        ),
        TestExecutionRecord(
            test_name="file missing",
            executed=True,
            exit_code=1,
            validation_method="file_check",
            validation_result={"ok": False, "exists": False},
            error="文件不存在",
        ),
        TestExecutionRecord(
            test_name="unsafe",
            executed=False,
            validation_method="command",
            validation_result={"ok": False, "reason": "command_rejected"},
            error="测试命令包含高风险 shell 字符",
        ),
    ]


def test_write_test_execution_report_persists_json_summary_and_records(tmp_path):
    report = write_test_execution_report(
        tmp_path,
        _mixed_records(),
        options=TestExecutionReportOptions(
            workspace_root=tmp_path / "workspace",
            timeout_seconds=12,
            executed_at="2026-05-08T12:00:00Z",
        ),
    )

    payload = json.loads(report.json_path.read_text(encoding="utf-8"))
    loaded = load_test_execution_report(report.json_path)

    assert report.total_tests == 3
    assert report.executed == 2
    assert report.passed == 1
    assert report.failed == 2
    assert payload["total_tests"] == 3
    assert payload["executed"] == 2
    assert payload["passed"] == 1
    assert payload["failed"] == 2
    assert payload["workspace_root"].endswith("workspace")
    assert payload["timeout_seconds"] == 12
    assert payload["records"][0]["test_name"] == "unit pass"
    assert loaded.records[2].error == "测试命令包含高风险 shell 字符"


def test_write_test_execution_report_persists_human_markdown(tmp_path):
    records = [
        TestExecutionRecord(
            test_name="content check",
            executed=True,
            validation_method="content_check",
            validation_result={"ok": True, "matched": True},
        ),
        TestExecutionRecord(
            test_name="blocked command",
            executed=False,
            validation_method="command",
            error="测试命令包含高风险 shell 字符",
            validation_result={"ok": False},
        ),
    ]

    report = write_test_execution_report(
        tmp_path,
        records,
        options=TestExecutionReportOptions(executed_at="2026-05-08T12:00:00Z"),
    )
    markdown = report.markdown_path.read_text(encoding="utf-8")

    assert "# 测试执行报告" in markdown
    assert "总数: 2" in markdown
    assert "通过: 1" in markdown
    assert "失败: 1" in markdown
    assert "PASS content check" in markdown
    assert "FAIL blocked command" in markdown
    assert "测试命令包含高风险 shell 字符" in markdown
