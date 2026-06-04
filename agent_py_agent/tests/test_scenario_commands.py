"""scenario_commands CLI 命令测试。

测试 scenario-test 命令及其辅助函数。
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest


class TestCmdScenarioTest:
    """测试 cmd_scenario_test 命令。"""

    def test_scenario_test_invalid_count(self, tmp_path: Path):
        """count <= 0 时返回错误。"""
        from agent_py_agent.cli.scenario import cmd_scenario_test

        args = MagicMock()
        args.case = "happy"
        args.count = 0
        args.max_runners = 2
        args.max_cycles = 3
        args.dry_run = False
        args.direct = False
        args.timeout = 300
        args.planner = False
        args.skill_dir = None
        args.workspace = None
        args.capability_config = str(tmp_path / "capability.yaml")

        result = cmd_scenario_test(args)
        assert result == 2

    def test_scenario_test_invalid_max_runners(self, tmp_path: Path):
        """max_runners <= 0 且非 dry_run 时返回错误。"""
        from agent_py_agent.cli.scenario import cmd_scenario_test

        args = MagicMock()
        args.case = "happy"
        args.count = 2
        args.max_runners = 0
        args.max_cycles = 3
        args.dry_run = False
        args.direct = False
        args.timeout = 300
        args.planner = False
        args.skill_dir = None
        args.workspace = None
        args.capability_config = str(tmp_path / "capability.yaml")

        result = cmd_scenario_test(args)
        assert result == 2

    def test_scenario_test_invalid_max_cycles(self, tmp_path: Path):
        """max_cycles <= 0 时返回错误。"""
        from agent_py_agent.cli.scenario import cmd_scenario_test

        args = MagicMock()
        args.case = "happy"
        args.count = 2
        args.max_runners = 2
        args.max_cycles = 0
        args.dry_run = False
        args.direct = False
        args.timeout = 300
        args.planner = False
        args.skill_dir = None
        args.workspace = None
        args.capability_config = str(tmp_path / "capability.yaml")

        result = cmd_scenario_test(args)
        assert result == 2


class TestScenarioUtils:
    """测试 scenario_utils 中的辅助函数。"""

    def test_build_scenario_prompt(self):
        """测试场景提示构建。"""
        from agent_py_agent.cli.scenario_utils import build_scenario_prompt

        prompt = build_scenario_prompt(count=2)

        assert prompt is not None
        assert len(prompt) > 0


class TestScenarioCaseRouting:
    """测试场景测试用例路由。"""

    def test_route_to_gateway_restart_case(self, tmp_path: Path):
        """路由到 gateway-restart case。"""
        from agent_py_agent.cli.scenario import cmd_scenario_test

        args = MagicMock()
        args.case = "gateway-restart"
        args.count = 1
        args.max_runners = 1
        args.max_cycles = 1
        args.dry_run = True
        args.direct = False
        args.timeout = 60
        args.planner = False
        args.skill_dir = None
        args.workspace = str(tmp_path)
        args.capability_config = str(tmp_path / "capability.yaml")

        with patch("agent_py_agent.cli.scenario.run_scenario_gateway_restart_case", return_value=0):
            result = cmd_scenario_test(args)
            assert result in (0, 2)


class TestPrintDispatchReport:
    """测试 print_dispatch_report 函数。"""

    def test_print_dispatch_report_basic(self, capsys):
        """测试调度报告打印。"""
        from agent_py_agent.cli.scenario import print_dispatch_report

        mock_record = MagicMock()
        mock_record.ok = True
        mock_record.run_id = "run_001"
        mock_record.step = 1
        mock_record.action = "test"
        mock_record.applied = True
        mock_record.message = "测试消息"

        mock_report = MagicMock()
        mock_report.summary = {"total": 1}
        mock_report.records = [mock_record]

        print_dispatch_report(mock_report)

        captured = capsys.readouterr()
        assert "summary=" in captured.out
        assert "[OK]" in captured.out


class TestScenarioDispatchLoop:
    """测试 happy-path dispatch 循环的等待行为。"""

    def test_dispatch_waits_between_cycles_when_children_are_running(self, monkeypatch):
        """后台 runner 还在 RUNNING 时，场景测试应等待下一轮，而不是瞬时耗尽 cycles。"""
        import agent_py_agent.cli.scenario as scenario

        sleeps: list[float] = []
        verified = iter([False, True])
        args = SimpleNamespace(
            capability_config="capability.yaml",
            skill_dir=None,
            max_cycles=2,
            count=1,
            max_runners=1,
            dry_run=False,
            planner=False,
            timeout=600,
        )
        report = SimpleNamespace(summary={}, records=[])
        request = scenario.ScenarioDispatchRequest(
            agent=SimpleNamespace(),
            args=args,
            paths=SimpleNamespace(config="agent.yaml"),
            created_via="gateway",
            gateway_payload={},
        )

        monkeypatch.setattr(scenario, "load_capability_config", lambda _path: object())
        monkeypatch.setattr(scenario, "make_capability_router", lambda *_args, **_kwargs: object())
        monkeypatch.setattr(scenario, "_run_scenario_dispatch_cycle", lambda *_args, **_kwargs: report)
        monkeypatch.setattr(scenario, "print_dispatch_report", lambda _report: None)
        monkeypatch.setattr(scenario, "print_scenario_board", lambda *_args, **_kwargs: None)
        monkeypatch.setattr(scenario, "scenario_tasks_verified", lambda *_args, **_kwargs: next(verified))
        monkeypatch.setattr(scenario, "scenario_tasks_active", lambda *_args, **_kwargs: True, raising=False)
        monkeypatch.setattr(
            scenario,
            "time",
            SimpleNamespace(sleep=lambda seconds: sleeps.append(seconds)),
            raising=False,
        )

        assert scenario._cmd_scenario_dispatch(request) is True
        assert sleeps and sleeps[0] > 0


class TestRunScenarioSuite:
    """测试 run_scenario_suite 函数。"""

    def test_run_scenario_suite_basic(self, tmp_path: Path):
        """测试场景套件运行。"""
        from agent_py_agent.cli.scenario import run_scenario_suite

        args = MagicMock()
        args.case = "all"
        args.count = 1
        args.max_runners = 1
        args.max_cycles = 1
        args.dry_run = True
        args.direct = False
        args.timeout = 60
        args.planner = False
        args.skill_dir = None
        args.workspace = str(tmp_path)
        args.capability_config = str(tmp_path / "capability.yaml")

        # Mock 各个 case 返回成功
        with patch("agent_py_agent.cli.scenario.cmd_scenario_test", return_value=0):
            result = run_scenario_suite(args)
            assert result == 0


class TestScenarioCases:
    """测试 scenario_cases 中的场景运行函数。"""

    def test_run_scenario_runner_retry_case(self, tmp_path: Path):
        """测试 runner-retry 场景。"""
        from agent_py_agent.cli.scenario import cmd_scenario_test

        args = MagicMock()
        args.case = "runner-retry"
        args.count = 1
        args.max_runners = 1
        args.max_cycles = 1
        args.dry_run = True
        args.direct = False
        args.timeout = 60
        args.planner = False
        args.skill_dir = None
        args.workspace = str(tmp_path)
        args.capability_config = str(tmp_path / "capability.yaml")

        with patch("agent_py_agent.cli.scenario.run_scenario_runner_retry_case", return_value=0):
            result = cmd_scenario_test(args)
            assert result in (0, 2)

    def test_run_scenario_structured_repair_case(self, tmp_path: Path):
        """测试 structured-repair 场景。"""
        from agent_py_agent.cli.scenario import cmd_scenario_test

        args = MagicMock()
        args.case = "structured-repair"
        args.count = 1
        args.max_runners = 1
        args.max_cycles = 1
        args.dry_run = True
        args.direct = False
        args.timeout = 60
        args.planner = False
        args.skill_dir = None
        args.workspace = str(tmp_path)
        args.capability_config = str(tmp_path / "capability.yaml")

        with patch("agent_py_agent.cli.scenario.run_scenario_structured_repair_case", return_value=0):
            result = cmd_scenario_test(args)
            assert result in (0, 2)
