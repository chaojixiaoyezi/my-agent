"""测试 subagents-tests CLI 命令。"""

import argparse
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from agent_py_agent.agent.subagents.execution_report import (
    TestExecutionReportOptions,
    write_test_execution_report,
)
from agent_py_agent.agent.subagents.models import TestExecutionRecord
from agent_py_agent.cli._review import cmd_subagents_acceptance, cmd_subagents_tests


class _FakeSubagents:
    def __init__(self, task):
        self._task = task
        self.review_options = None
        self.workspace = Path(task.reports_dir).parent

    def load(self, run_id):
        assert run_id == self._task.id
        return self._task

    def write_acceptance_review_report(self, run_ids=None, options=None):
        self.review_options = options
        return SimpleNamespace(records=[], summary={})


class _FakeAgent:
    def __init__(self, task, *, acceptance_execute_tests=False, acceptance_test_timeout_seconds=120):
        self.config = SimpleNamespace(
            acceptance_execute_tests=acceptance_execute_tests,
            acceptance_test_timeout_seconds=acceptance_test_timeout_seconds,
        )
        self.subagents = _FakeSubagents(task)


def _args(tmp_path, *, re_run=False):
    return argparse.Namespace(
        config=str(tmp_path / "config.yaml"),
        run_id="run-1",
        re_run=re_run,
        timeout=7,
    )


def _task(tmp_path):
    reports = tmp_path / "reports"
    reports.mkdir()
    output_json = tmp_path / "output.json"
    output_json.write_text(json.dumps({
        "tests": [{
            "name": "file exists",
            "validation_method": "file_check",
            "file_path": "ok.txt",
        }]
    }), encoding="utf-8")
    (tmp_path / "ok.txt").write_text("ok\n", encoding="utf-8")
    return SimpleNamespace(id="run-1", reports_dir=str(reports), output_json=str(output_json))


def test_subagents_tests_prints_existing_report(tmp_path, capsys):
    task = _task(tmp_path)
    write_test_execution_report(
        task.reports_dir,
        [TestExecutionRecord(test_name="unit", executed=True, validation_result={"ok": True})],
        options=TestExecutionReportOptions(executed_at="2026-05-08T12:00:00Z"),
    )

    with patch("agent_py_agent.cli._review.make_agent", return_value=_FakeAgent(task)):
        result = cmd_subagents_tests(_args(tmp_path))

    out = capsys.readouterr().out
    assert result == 0
    assert "SUBAGENT TESTS" in out
    assert "run_id=run-1" in out
    assert "total=1 executed=1 passed=1 failed=0" in out
    assert "unit" in out


def test_subagents_tests_rerun_invokes_explicit_acceptance_execution(tmp_path, capsys):
    task = _task(tmp_path)
    fake_agent = _FakeAgent(task)

    with patch("agent_py_agent.cli._review.make_agent", return_value=fake_agent):
        result = cmd_subagents_tests(_args(tmp_path, re_run=True))

    out = capsys.readouterr().out
    assert result == 0
    assert fake_agent.subagents.review_options.execute_tests is True
    assert fake_agent.subagents.review_options.test_timeout_seconds == 7
    assert Path(task.reports_dir, "test_execution.json").exists()
    assert "re_run=True" in out
    assert "file exists" in out


def test_subagents_acceptance_uses_configured_real_test_defaults(tmp_path):
    task = _task(tmp_path)
    fake_agent = _FakeAgent(task, acceptance_execute_tests=True, acceptance_test_timeout_seconds=11)
    args = argparse.Namespace(
        config=str(tmp_path / "config.yaml"),
        run_id=["run-1"],
        apply=False,
        reviewer="tester",
        note="",
        limit=20,
        execute_tests=None,
        test_timeout=None,
    )

    with patch("agent_py_agent.cli._review.make_agent", return_value=fake_agent):
        result = cmd_subagents_acceptance(args)

    assert result == 0
    assert fake_agent.subagents.review_options.execute_tests is True
    assert fake_agent.subagents.review_options.test_timeout_seconds == 11


def test_subagents_acceptance_cli_override_wins_over_config(tmp_path):
    task = _task(tmp_path)
    fake_agent = _FakeAgent(task, acceptance_execute_tests=True, acceptance_test_timeout_seconds=11)
    args = argparse.Namespace(
        config=str(tmp_path / "config.yaml"),
        run_id=["run-1"],
        apply=False,
        reviewer="tester",
        note="",
        limit=20,
        execute_tests=False,
        test_timeout=5,
    )

    with patch("agent_py_agent.cli._review.make_agent", return_value=fake_agent):
        result = cmd_subagents_acceptance(args)

    assert result == 0
    assert fake_agent.subagents.review_options.execute_tests is False
    assert fake_agent.subagents.review_options.test_timeout_seconds == 5
