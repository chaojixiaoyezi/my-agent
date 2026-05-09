"""CLI 子代理子命令测试。

测试 agent_py_agent/cli/subcommands_agents.py 中的子命令注册函数。
验证子代理相关命令（list、show、run、workflow）参数解析正确。
"""
from __future__ import annotations

import argparse
from unittest.mock import MagicMock, patch

import pytest


class TestSubagentsSubcommandRegistration:
    """测试子代理子命令注册功能。"""

    def test_add_subagents_subcommands_creates_expected_commands(self):
        """测试子代理子命令注册创建预期的命令。

        验证所有子代理子命令都能被正确注册到解析器。
        """
        from agent_py_agent.cli.subcommands_agents import add_subagents_subcommands

        parser = argparse.ArgumentParser()
        sub = parser.add_subparsers(dest="subcommand")

        add_subagents_subcommands(sub)

        # 验证所有预期的子命令都存在
        expected_commands = [
            "spawn-subagents",
            "subagents",
            "subagents-workflow-plan",
            "subagents-due-check",
            "subagents-probe",
            "subagents-plan-actions",
            "subagents-apply-actions",
            "subagents-route-capabilities",
            "subagents-acceptance",
            "subagents-acceptance-plan",
            "subagents-tests",
            "subagents-patches",
            "subagents-dispatch",
            "subagent-context",
            "subagent-run",
            "subagent",
        ]

        for cmd in expected_commands:
            assert cmd in parser._subparsers._actions[1].choices, f"Command {cmd} not found"

    def test_spawn_subagents_has_goal_argument(self):
        """测试 spawn-subagents 命令有 goal 参数。

        验证 goal 参数被正确添加。
        """
        from agent_py_agent.cli.subcommands_agents import add_subagents_subcommands

        parser = argparse.ArgumentParser()
        sub = parser.add_subparsers(dest="subcommand")
        add_subagents_subcommands(sub)

        args = parser.parse_args(["spawn-subagents", "测试目标"])
        assert args.goal == "测试目标"

    def test_spawn_subagents_has_count_argument(self):
        """测试 spawn-subagents 命令有 count 参数。

        验证 count 参数默认值为 3。
        """
        from agent_py_agent.cli.subcommands_agents import add_subagents_subcommands

        parser = argparse.ArgumentParser()
        sub = parser.add_subparsers(dest="subcommand")
        add_subagents_subcommands(sub)

        args = parser.parse_args(["spawn-subagents", "测试"])
        assert hasattr(args, "count")
        assert args.count == 3

    def test_subagents_has_limit_argument(self):
        """测试 subagents 命令有 limit 参数。

        验证 limit 参数正确工作。
        """
        from agent_py_agent.cli.subcommands_agents import add_subagents_subcommands

        parser = argparse.ArgumentParser()
        sub = parser.add_subparsers(dest="subcommand")
        add_subagents_subcommands(sub)

        args = parser.parse_args(["subagents", "--limit", "50"])
        assert args.limit == 50

    def test_subagents_has_all_flag(self):
        """测试 subagents 命令有 --all 标志。

        验证 --all 标志正确工作。
        """
        from agent_py_agent.cli.subcommands_agents import add_subagents_subcommands

        parser = argparse.ArgumentParser()
        sub = parser.add_subparsers(dest="subcommand")
        add_subagents_subcommands(sub)

        args = parser.parse_args(["subagents", "--all"])
        assert args.all is True

    def test_subagents_dispatch_has_dry_run_flag(self):
        """测试 subagents-dispatch 命令有 --dry-run 标志。

        验证 dry-run 标志正确工作。
        """
        from agent_py_agent.cli.subcommands_agents import add_subagents_subcommands

        parser = argparse.ArgumentParser()
        sub = parser.add_subparsers(dest="subcommand")
        add_subagents_subcommands(sub)

        args = parser.parse_args(["subagents-dispatch", "--dry-run"])
        assert args.apply is False

    def test_subagents_dispatch_has_apply_flag(self):
        """测试 subagents-dispatch 命令有 --apply 标志。

        验证 apply 标志正确工作。
        """
        from agent_py_agent.cli.subcommands_agents import add_subagents_subcommands

        parser = argparse.ArgumentParser()
        sub = parser.add_subparsers(dest="subcommand")
        add_subagents_subcommands(sub)

        args = parser.parse_args(["subagents-dispatch", "--apply"])
        assert args.apply is True

    def test_subagents_dispatch_has_execute_acceptance_tests_argument(self):
        """测试 subagents-dispatch 可显式触发父级验收 tests，但不自动 apply。"""
        from agent_py_agent.cli.subcommands_agents import add_subagents_subcommands

        parser = argparse.ArgumentParser()
        sub = parser.add_subparsers(dest="subcommand")
        add_subagents_subcommands(sub)

        args = parser.parse_args(["subagents-dispatch", "--execute-acceptance-tests"])
        assert args.execute_acceptance_tests is True

    def test_subagent_run_has_run_id_argument(self):
        """测试 subagent-run 命令有 run_id 参数。

        验证 run_id 参数正确工作。
        """
        from agent_py_agent.cli.subcommands_agents import add_subagents_subcommands

        parser = argparse.ArgumentParser()
        sub = parser.add_subparsers(dest="subcommand")
        add_subagents_subcommands(sub)

        args = parser.parse_args(["subagent-run", "run-123"])
        assert args.run_id == "run-123"

    def test_subagent_has_run_id_argument(self):
        """测试 subagent 命令有 run_id 参数。

        验证 run_id 参数正确工作。
        """
        from agent_py_agent.cli.subcommands_agents import add_subagents_subcommands

        parser = argparse.ArgumentParser()
        sub = parser.add_subparsers(dest="subcommand")
        add_subagents_subcommands(sub)

        args = parser.parse_args(["subagent", "run-456"])
        assert args.run_id == "run-456"

    def test_subagents_probe_accepts_run_id_list(self):
        """测试 subagents-probe 命令接受 run_id 列表。

        验证可以传入多个 run_id。
        """
        from agent_py_agent.cli.subcommands_agents import add_subagents_subcommands

        parser = argparse.ArgumentParser()
        sub = parser.add_subparsers(dest="subcommand")
        add_subagents_subcommands(sub)

        args = parser.parse_args(["subagents-probe", "run-1", "run-2", "run-3"])
        assert args.run_id == ["run-1", "run-2", "run-3"]






class TestSubagentsReviewCommandRegistration:
    """测试子代理子命令注册功能。"""

    def test_subagents_workflow_plan_has_goal_argument(self):
        """测试 subagents-workflow-plan 命令有 goal 参数。

        验证 goal 参数正确工作。
        """
        from agent_py_agent.cli.subcommands_agents import add_subagents_subcommands

        parser = argparse.ArgumentParser()
        sub = parser.add_subparsers(dest="subcommand")
        add_subagents_subcommands(sub)

        args = parser.parse_args(["subagents-workflow-plan", "测试工作流目标"])
        assert args.goal == "测试工作流目标"

    def test_add_capability_config_arg(self):
        """测试 _add_capability_config_arg 函数。

        验证 capability-config 参数被正确添加。
        """
        from agent_py_agent.cli.subcommands_agents import _add_capability_config_arg

        parser = argparse.ArgumentParser()
        _add_capability_config_arg(parser)

        # 验证默认值为 DEFAULT_CAPABILITY_CONFIG
        from agent_py_agent.cli.common import DEFAULT_CAPABILITY_CONFIG
        args = parser.parse_args([])
        assert args.capability_config == str(DEFAULT_CAPABILITY_CONFIG)

    def test_subagents_acceptance_has_review_args(self):
        """测试 subagents-acceptance 命令有审核相关参数。

        验证 reviewer 和 note 参数正确工作。
        """
        from agent_py_agent.cli.subcommands_agents import add_subagents_subcommands

        parser = argparse.ArgumentParser()
        sub = parser.add_subparsers(dest="subcommand")
        add_subagents_subcommands(sub)

        args = parser.parse_args([
            "subagents-acceptance",
            "--reviewer", "test-reviewer",
            "--note", "测试备注"
        ])
        assert args.reviewer == "test-reviewer"
        assert args.note == "测试备注"

    def test_subagents_acceptance_has_real_test_override_args(self):
        """测试 subagents-acceptance 命令可以显式覆盖真实测试执行配置。"""
        from agent_py_agent.cli.subcommands_agents import add_subagents_subcommands

        parser = argparse.ArgumentParser()
        sub = parser.add_subparsers(dest="subcommand")
        add_subagents_subcommands(sub)

        args = parser.parse_args([
            "subagents-acceptance",
            "--execute-tests",
            "--test-timeout", "9",
        ])
        assert args.execute_tests is True
        assert args.test_timeout == 9

        args = parser.parse_args([
            "subagents-acceptance",
            "--no-execute-tests",
        ])
        assert args.execute_tests is False

    def test_subagents_tests_has_view_and_rerun_args(self):
        """测试 subagents-tests 命令有查看和显式重跑参数。"""
        from agent_py_agent.cli.subcommands_agents import add_subagents_subcommands

        parser = argparse.ArgumentParser()
        sub = parser.add_subparsers(dest="subcommand")
        add_subagents_subcommands(sub)

        args = parser.parse_args([
            "subagents-tests",
            "run-123",
            "--re-run",
            "--timeout", "9",
        ])
        assert args.run_id == "run-123"
        assert args.re_run is True
        assert args.timeout == 9

    def test_subagents_acceptance_plan_has_run_id_argument(self):
        """测试 subagents-acceptance-plan 命令必须指定 run_id。"""
        from agent_py_agent.cli.subcommands_agents import add_subagents_subcommands

        parser = argparse.ArgumentParser()
        sub = parser.add_subparsers(dest="subcommand")
        add_subagents_subcommands(sub)

        args = parser.parse_args(["subagents-acceptance-plan", "run-123"])
        assert args.run_id == "run-123"

    def test_subagents_acceptance_plan_has_write_argument(self):
        """测试 subagents-acceptance-plan 可显式写入 dry-run 决策文件。"""
        from agent_py_agent.cli.subcommands_agents import add_subagents_subcommands

        parser = argparse.ArgumentParser()
        sub = parser.add_subparsers(dest="subcommand")
        add_subagents_subcommands(sub)

        args = parser.parse_args(["subagents-acceptance-plan", "run-123", "--write"])
        assert args.write is True

    def test_subagents_acceptance_plan_has_apply_argument(self):
        """测试 subagents-acceptance-plan 可显式 apply 低风险决策。"""
        from agent_py_agent.cli.subcommands_agents import add_subagents_subcommands

        parser = argparse.ArgumentParser()
        sub = parser.add_subparsers(dest="subcommand")
        add_subagents_subcommands(sub)

        args = parser.parse_args(["subagents-acceptance-plan", "run-123", "--apply"])
        assert args.apply is True

    def test_subagents_acceptance_plan_has_next_action_argument(self):
        """测试 subagents-acceptance-plan 可查看父级下一动作建议。"""
        from agent_py_agent.cli.subcommands_agents import add_subagents_subcommands

        parser = argparse.ArgumentParser()
        sub = parser.add_subparsers(dest="subcommand")
        add_subagents_subcommands(sub)

        args = parser.parse_args(["subagents-acceptance-plan", "run-123", "--next-action"])
        assert args.next_action is True

    def test_subagents_acceptance_plan_has_auto_policy_argument(self):
        """测试 subagents-acceptance-plan 可查看自动策略 dry-run。"""
        from agent_py_agent.cli.subcommands_agents import add_subagents_subcommands

        parser = argparse.ArgumentParser()
        sub = parser.add_subparsers(dest="subcommand")
        add_subagents_subcommands(sub)

        args = parser.parse_args(["subagents-acceptance-plan", "run-123", "--auto-policy"])
        assert args.auto_policy is True

    def test_subagents_acceptance_plan_has_auto_execution_argument(self):
        """测试 subagents-acceptance-plan 可查看自动执行 dry-run facade。"""
        from agent_py_agent.cli.subcommands_agents import add_subagents_subcommands

        parser = argparse.ArgumentParser()
        sub = parser.add_subparsers(dest="subcommand")
        add_subagents_subcommands(sub)

        args = parser.parse_args(["subagents-acceptance-plan", "run-123", "--auto-execution"])
        assert args.auto_execution is True

    def test_subagents_acceptance_plan_has_execute_auto_tests_argument(self):
        """测试 subagents-acceptance-plan 自动执行 facade 需要显式测试执行确认。"""
        from agent_py_agent.cli.subcommands_agents import add_subagents_subcommands

        parser = argparse.ArgumentParser()
        sub = parser.add_subparsers(dest="subcommand")
        add_subagents_subcommands(sub)

        args = parser.parse_args([
            "subagents-acceptance-plan",
            "run-123",
            "--auto-execution",
            "--execute-auto-tests",
        ])
        assert args.execute_auto_tests is True

    def test_subagents_patches_has_patch_action_group(self):
        """测试 subagents-patches 命令有 patch 操作组。

        验证 mutually exclusive group 正确工作。
        """
        from agent_py_agent.cli.subcommands_agents import add_subagents_subcommands

        parser = argparse.ArgumentParser()
        sub = parser.add_subparsers(dest="subcommand")
        add_subagents_subcommands(sub)

        # 默认值
        args = parser.parse_args(["subagents-patches"])
        assert args.patch_action == "review_dry_run"

        # --apply
        args = parser.parse_args(["subagents-patches", "--apply"])
        assert args.patch_action == "apply"


class TestSubagentWorkflowPlanCommand:
    """测试子代理工作流计划命令。"""

    def test_workflow_plan_with_template_id(self):
        """测试带 template-id 的工作流计划。

        验证 --template-id 参数正确工作。
        """
        from agent_py_agent.cli.subcommands_agents import add_subagents_subcommands

        parser = argparse.ArgumentParser()
        sub = parser.add_subparsers(dest="subcommand")
        add_subagents_subcommands(sub)

        args = parser.parse_args([
            "subagents-workflow-plan",
            "测试目标",
            "--template-id", "my-template"
        ])
        assert args.template_id == "my-template"

    def test_workflow_plan_with_output_dir(self):
        """测试带 output-dir 的工作流计划。

        验证 --output-dir 参数正确工作。
        """
        from agent_py_agent.cli.subcommands_agents import add_subagents_subcommands

        parser = argparse.ArgumentParser()
        sub = parser.add_subparsers(dest="subcommand")
        add_subagents_subcommands(sub)

        args = parser.parse_args([
            "subagents-workflow-plan",
            "测试",
            "--output-dir", "/tmp/output"
        ])
        assert args.output_dir == "/tmp/output"

    def test_workflow_plan_with_json_flag(self):
        """测试带 --json 的工作流计划。

        验证 --json 标志正确工作。
        """
        from agent_py_agent.cli.subcommands_agents import add_subagents_subcommands

        parser = argparse.ArgumentParser()
        sub = parser.add_subparsers(dest="subcommand")
        add_subagents_subcommands(sub)

        args = parser.parse_args(["subagents-workflow-plan", "测试", "--json"])
        assert args.json is True
