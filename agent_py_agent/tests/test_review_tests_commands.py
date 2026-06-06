from __future__ import annotations

from types import SimpleNamespace

from agent_py_agent.agent.subagents.execution.records import TestExecutionRecord
from agent_py_agent.agent.subagents.execution.report import (
    TestExecutionReportOptions,
    write_test_execution_report,
)
from agent_py_agent.cli import _review_tests


class _Subagents:
    def __init__(self, task: SimpleNamespace) -> None:
        self._task = task
        self.workspace_root = ""

    def load(self, _run_id: str) -> SimpleNamespace:
        return self._task


def test_subagents_tests_reports_bad_output_json(tmp_path, capsys):
    reports_dir = tmp_path / "reports"
    output_json = tmp_path / "output.json"
    output_json.write_text("{bad json", encoding="utf-8")
    write_test_execution_report(
        reports_dir,
        [
            TestExecutionRecord(
                test_name="smoke",
                executed=True,
                validation_method="file_check",
                validation_result={"ok": True},
            )
        ],
        options=TestExecutionReportOptions(workspace_root=tmp_path),
    )
    task = SimpleNamespace(
        reports_dir=str(reports_dir),
        output_json=str(output_json),
    )
    agent = SimpleNamespace(subagents=_Subagents(task))

    code = _review_tests.cmd_subagents_tests(
        SimpleNamespace(run_id="child-1", re_run=False, timeout=1),
        make_agent_fn=lambda _args: agent,
    )

    out = capsys.readouterr().out
    assert code == 0
    assert "output_load_error" in out
    assert "cli.subagents_tests.output_json" in out
