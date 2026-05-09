"""Regression tests for subagents-tests rerun guardrails."""

import argparse
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from agent_py_agent.agent.subagents.execution_report import (
    TestExecutionReportOptions,
    write_test_execution_report,
)
from agent_py_agent.cli._review import cmd_subagents_tests


class _FakeSubagents:
    """LLM: Minimal fake manager for rerun guard behavior without acceptance service noise."""

    # 函数用途: 保存一个 task，并让测试替身控制 acceptance 是否写空报告。
    def __init__(self, task):
        self._task = task
        self.workspace_root = Path(task.output_json).parent
        self.review_options = None

    # 函数用途: 模拟 manager.load，只允许当前测试 run_id。
    def load(self, run_id):
        assert run_id == self._task.id
        return self._task

    # 函数用途: 默认 acceptance 不写报告，交给 CLI fallback 真实执行。
    def write_acceptance_review_report(self, run_ids=None, options=None):
        self.review_options = options
        return SimpleNamespace(records=[], summary={})


class _FakeAgent:
    """LLM: Minimal fake agent exposing config and subagents for cmd_subagents_tests."""

    # 函数用途: 组合 CLI 命令所需的 config 和 subagents 两个字段。
    def __init__(self, task):
        self.config = SimpleNamespace()
        self.subagents = _FakeSubagents(task)


def _args(tmp_path, *, re_run=False):
    """LLM: Build argparse-like inputs for the subagents-tests command."""

    return argparse.Namespace(
        config=str(tmp_path / "config.yaml"),
        run_id="run-1",
        re_run=re_run,
        timeout=7,
    )


def _task(tmp_path):
    """LLM: Create a task with one declared file_check test and one matching artifact."""

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


def test_subagents_tests_rerun_does_not_accept_empty_acceptance_report(tmp_path, capsys):
    """LLM: A zero-test acceptance side effect must not mask declared output tests."""

    task = _task(tmp_path)
    fake_agent = _FakeAgent(task)

    def write_empty_report(run_ids=None, options=None):
        fake_agent.subagents.review_options = options
        write_test_execution_report(
            task.reports_dir,
            [],
            options=TestExecutionReportOptions(executed_at="2026-05-09T09:00:00Z"),
        )
        return SimpleNamespace(records=[], summary={})

    fake_agent.subagents.write_acceptance_review_report = write_empty_report

    with patch("agent_py_agent.cli._review.make_agent", return_value=fake_agent):
        result = cmd_subagents_tests(_args(tmp_path, re_run=True))

    out = capsys.readouterr().out
    assert result == 0
    assert "total=1 executed=1 passed=1 failed=0" in out
    assert "file exists" in out


def test_subagents_tests_returns_failure_for_empty_existing_report(tmp_path, capsys):
    """LLM: Viewing an existing zero-test report should return nonzero instead of fake success."""

    task = _task(tmp_path)
    write_test_execution_report(
        task.reports_dir,
        [],
        options=TestExecutionReportOptions(executed_at="2026-05-09T09:00:00Z"),
    )

    with patch("agent_py_agent.cli._review.make_agent", return_value=_FakeAgent(task)):
        result = cmd_subagents_tests(_args(tmp_path))

    out = capsys.readouterr().out
    assert result == 1
    assert "total=0 executed=0 passed=0 failed=0" in out
