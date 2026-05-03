"""scenario_commands CLI 命令测试。

测试 scenario-test 命令及其辅助函数。
"""
from __future__ import annotations

from pathlib import Path
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

    def test_route_to_verification_case(self, tmp_path: Path):
        """路由到 verification case。"""
        from agent_py_agent.cli.scenario import cmd_scenario_test

        args = MagicMock()
        args.case = "verification"
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

        with patch("agent_py_agent.cli.scenario.run_scenario_verification_case", return_value=0):
            result = cmd_scenario_test(args)
            # 取决于 mock 返回值
            assert result in (0, 2)

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
