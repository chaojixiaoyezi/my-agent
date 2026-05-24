"""Tests for parent-owned subagent test packs."""

import argparse
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from agent_py_agent.agent.subagents.parent_test_pack import (
    ParentTestPackWriteRequest,
    load_parent_test_items,
    write_parent_test_pack,
)
from agent_py_agent.cli._review import cmd_subagents_tests


class _FakeSubagents:
    """LLM: Minimal manager fake that lets subagents-tests fall back to direct execution."""

    # 函数用途: 保存当前 task，并暴露 workspace_root 给测试执行器限制目录。
    def __init__(self, task, workspace_root: Path):
        self._task = task
        self.workspace_root = workspace_root

    # 函数用途: 模拟根据 run_id 读取 task。
    def load(self, run_id):
        assert run_id == self._task.id
        return self._task

    # 函数用途: 模拟 acceptance 侧没有提前写测试报告。
    def write_acceptance_review_report(self, run_ids=None, options=None):
        return SimpleNamespace(records=[], summary={})


def test_parent_test_pack_round_trips_parent_owned_tests(tmp_path):
    """LLM: Parent test packs should load tests from reports without trusting worker output."""

    task = _task(tmp_path)

    write_parent_test_pack(
        ParentTestPackWriteRequest(
            reports_dir=task.reports_dir,
            tests=[{
                "name": "parent checkout contract",
                "validation_method": "command",
                "command": "python3 -m pytest tests/parent/test_checkout_contract.py -q",
                "working_dir": "sample-webapp",
            }],
            source="parent_oracle",
        )
    )

    loaded = load_parent_test_items(task)

    assert loaded == [{
        "name": "parent checkout contract",
        "validation_method": "command",
        "command": "python3 -m pytest tests/parent/test_checkout_contract.py -q",
        "working_dir": "sample-webapp",
        "source": "parent_oracle",
    }]


def test_subagents_tests_rerun_executes_parent_owned_web_pack(tmp_path, capsys):
    """LLM: subagents-tests should run parent-owned sample-webapp checks even when output tests are empty."""

    task = _task(tmp_path)
    _write_parent_contract(tmp_path / "sample-webapp")
    write_parent_test_pack(
        ParentTestPackWriteRequest(
            reports_dir=task.reports_dir,
            tests=[{
                "name": "parent shop contract",
                "validation_method": "command",
                "command": "python3 -m pytest tests/parent/test_shop_contract.py -q",
                "working_dir": "sample-webapp",
            }],
            source="shop_parent_oracle",
        )
    )
    agent = SimpleNamespace(subagents=_FakeSubagents(task, tmp_path), config=SimpleNamespace())

    with patch("agent_py_agent.cli._review.make_agent", return_value=agent):
        result = cmd_subagents_tests(_args(tmp_path, re_run=True))

    out = capsys.readouterr().out
    assert result == 0
    assert "total=1 executed=1 passed=1 failed=0" in out
    assert "parent shop contract" in out
    assert "classification=passed recommended_action=apply_acceptance" in out


def test_subagents_tests_rerun_replaces_empty_acceptance_report_with_parent_pack(tmp_path, capsys):
    """LLM: Empty acceptance side effects must not mask parent-owned test packs."""

    task = _task(tmp_path)
    _write_parent_contract(tmp_path / "sample-webapp")
    write_parent_test_pack(
        ParentTestPackWriteRequest(
            reports_dir=task.reports_dir,
            tests=[{
                "name": "parent shop contract",
                "validation_method": "command",
                "command": "python3 -m pytest tests/parent/test_shop_contract.py -q",
                "working_dir": "sample-webapp",
            }],
            source="shop_parent_oracle",
        )
    )
    fake_subagents = _FakeSubagents(task, tmp_path)

    def write_empty_report(run_ids=None, options=None):
        from agent_py_agent.agent.subagents.execution_report import write_test_execution_report

        write_test_execution_report(task.reports_dir, [])
        return SimpleNamespace(records=[], summary={})

    fake_subagents.write_acceptance_review_report = write_empty_report
    agent = SimpleNamespace(subagents=fake_subagents, config=SimpleNamespace())

    with patch("agent_py_agent.cli._review.make_agent", return_value=agent):
        result = cmd_subagents_tests(_args(tmp_path, re_run=True))

    out = capsys.readouterr().out
    assert result == 0
    assert "total=1 executed=1 passed=1 failed=0" in out
    assert "parent shop contract" in out


def _task(tmp_path):
    """LLM: Build a task whose worker output intentionally omits tests."""

    reports = tmp_path / "reports"
    reports.mkdir()
    output_json = tmp_path / "output.json"
    output_json.write_text(json.dumps({"tests": [], "artifacts": []}), encoding="utf-8")
    return SimpleNamespace(id="run-1", reports_dir=str(reports), output_json=str(output_json))


def _args(tmp_path, *, re_run=False):
    """LLM: Build argparse-like args for cmd_subagents_tests."""

    return argparse.Namespace(
        config=str(tmp_path / "config.yaml"),
        run_id="run-1",
        re_run=re_run,
        timeout=10,
    )


def _write_parent_contract(webapp: Path) -> None:
    """LLM: Create a tiny sample-webapp fixture and parent-owned pytest contract."""

    test_dir = webapp / "tests" / "parent"
    test_dir.mkdir(parents=True)
    (webapp / "index.html").write_text(
        '<button data-action="login">Login</button><img src="assets/product.svg" alt="Product">\n',
        encoding="utf-8",
    )
    (webapp / "assets").mkdir()
    (webapp / "assets" / "product.svg").write_text("<svg></svg>\n", encoding="utf-8")
    (test_dir / "test_shop_contract.py").write_text(
        "from pathlib import Path\n\n"
        "def test_login_button_and_image_refs_exist():\n"
        "    root = Path.cwd()\n"
        "    html = (root / 'index.html').read_text()\n"
        "    assert 'data-action=\"login\"' in html\n"
        "    assert (root / 'assets' / 'product.svg').is_file()\n",
        encoding="utf-8",
    )
