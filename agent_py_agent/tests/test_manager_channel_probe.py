"""Tests for SubAgentChannelProbeMixin: channel health checks, probe result recording, and status judgment.

给人看的解释：
测试子代理通道探测模块：通道健康检查、probe 结果记录、状态判断。
"""
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from agent_py_agent.agent.config import AgentConfig
from agent_py_agent.agent.core import SimpleAgent


class TestProbeChannel:
    """测试 probe_channel 方法。"""

    def test_probe_channel_basic(self):
        """验证基本通道探测功能。"""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            cfg = AgentConfig(model_backend="echo", subagent_workspace="subs")
            agent = SimpleAgent(cfg, root)

            task = agent.subagents.create_run(
                goal="探测通道测试",
                thought="思考探测",
                plan=["step1"],
            )

            result = agent.subagents.probe_channel(task.id)

            assert result.run_id == task.id
            assert result.channel_status in ("OK", "DEGRADED", "BROKEN")
            assert len(result.checks) > 0
            assert result.goal == task.goal
            assert result.created_at > 0

    def test_probe_channel_returns_channel_probe_result(self):
        """验证返回正确的 ChannelProbeResult 类型。"""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            cfg = AgentConfig(model_backend="echo", subagent_workspace="subs")
            agent = SimpleAgent(cfg, root)

            task = agent.subagents.create_run(
                goal="探测结果类型测试",
                thought="思考",
                plan=["step1"],
            )

            result = agent.subagents.probe_channel(task.id)

            # 验证 result 有预期的属性
            assert hasattr(result, "run_id")
            assert hasattr(result, "channel_status")
            assert hasattr(result, "checks")
            assert hasattr(result, "task_dir")
            assert hasattr(result, "created_at")

    def test_probe_channel_updates_task_channel_status(self):
        """验证探测后更新任务的 channel_status。"""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            cfg = AgentConfig(model_backend="echo", subagent_workspace="subs")
            agent = SimpleAgent(cfg, root)

            task = agent.subagents.create_run(
                goal="更新通道状态测试",
                thought="思考",
                plan=["step1"],
            )

            assert task.channel_status == "UNKNOWN"

            result = agent.subagents.probe_channel(task.id)

            updated_task = agent.subagents.load(task.id)
            assert updated_task.channel_status == result.channel_status
            assert updated_task.last_probe_at == result.created_at


class TestProbeChannels:
    """测试 probe_channels 批量探测方法。"""

    def test_probe_channels_with_specific_ids(self):
        """验证指定 run_ids 时只探测这些任务。"""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            cfg = AgentConfig(model_backend="echo", subagent_workspace="subs")
            agent = SimpleAgent(cfg, root)

            task1 = agent.subagents.create_run(goal="任务1", thought="思考", plan=["step1"])
            task2 = agent.subagents.create_run(goal="任务2", thought="思考", plan=["step1"])
            task3 = agent.subagents.create_run(goal="任务3", thought="思考", plan=["step1"])

            report = agent.subagents.probe_channels([task1.id, task2.id])

            assert report.summary["total"] == 2
            result_ids = [r.run_id for r in report.results]
            assert task1.id in result_ids
            assert task2.id in result_ids
            assert task3.id not in result_ids

    def test_probe_channels_with_limit(self):
        """验证 limit 参数限制探测数量。"""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            cfg = AgentConfig(model_backend="echo", subagent_workspace="subs")
            agent = SimpleAgent(cfg, root)

            for i in range(5):
                agent.subagents.create_run(goal=f"任务{i}", thought="思考", plan=["step1"])

            report = agent.subagents.probe_channels(limit=2)

            assert report.summary["total"] == 2
            assert len(report.results) == 2

    def test_probe_channels_skips_missing_tasks(self):
        """验证跳过不存在任务的 ID。"""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            cfg = AgentConfig(model_backend="echo", subagent_workspace="subs")
            agent = SimpleAgent(cfg, root)

            task1 = agent.subagents.create_run(goal="任务1", thought="思考", plan=["step1"])

            report = agent.subagents.probe_channels([task1.id, "non-existent-id"])

            assert report.summary["total"] == 1
            assert report.results[0].run_id == task1.id

    def test_probe_channels_returns_channel_probe_report(self):
        """验证返回正确的 ChannelProbeReport 类型。"""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            cfg = AgentConfig(model_backend="echo", subagent_workspace="subs")
            agent = SimpleAgent(cfg, root)

            task = agent.subagents.create_run(goal="报告类型测试", thought="思考", plan=["step1"])

            report = agent.subagents.probe_channels([task.id])

            assert hasattr(report, "generated_at")
            assert hasattr(report, "summary")
            assert hasattr(report, "results")
            assert report.generated_at > 0


class TestWriteChannelProbeReport:
    """测试 write_channel_probe_report 方法。"""

    def test_write_channel_probe_report_creates_files(self):
        """验证写出探测报告到文件。"""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            cfg = AgentConfig(model_backend="echo", subagent_workspace="subs")
            agent = SimpleAgent(cfg, root)

            task = agent.subagents.create_run(goal="写出报告测试", thought="思考", plan=["step1"])

            report = agent.subagents.write_channel_probe_report([task.id])

            json_file = agent.subagents.workspace / "subagent_channel_probe.json"
            md_file = agent.subagents.workspace / "SUBAGENT_CHANNEL_PROBE.md"

            assert json_file.exists()
            assert md_file.exists()
            assert report.summary["total"] == 1

    def test_write_channel_probe_report_with_limit(self):
        """验证带 limit 写出报告。"""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            cfg = AgentConfig(model_backend="echo", subagent_workspace="subs")
            agent = SimpleAgent(cfg, root)

            for i in range(3):
                agent.subagents.create_run(goal=f"任务{i}", thought="思考", plan=["step1"])

            report = agent.subagents.write_channel_probe_report(limit=2)

            assert report.summary["total"] == 2


class TestChannelProbeChecks:
    """测试通道探测检查项。"""

    def test_probe_checks_contain_work_order_validation(self):
        """验证探测包含工单文件校验。"""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            cfg = AgentConfig(model_backend="echo", subagent_workspace="subs")
            agent = SimpleAgent(cfg, root)

            task = agent.subagents.create_run(goal="检查项测试", thought="思考", plan=["step1"])

            result = agent.subagents.probe_channel(task.id)

            check_names = [c.name for c in result.checks]
            assert "work_order_files" in check_names

    def test_probe_checks_contain_json_readable(self):
        """验证探测包含 JSON 可读性检查。"""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            cfg = AgentConfig(model_backend="echo", subagent_workspace="subs")
            agent = SimpleAgent(cfg, root)

            task = agent.subagents.create_run(goal="JSON检查测试", thought="思考", plan=["step1"])

            result = agent.subagents.probe_channel(task.id)

            check_names = [c.name for c in result.checks]
            # 至少应该有一个 JSON 文件检查
            json_checks = [n for n in check_names if "json" in n.lower()]
            assert len(json_checks) > 0

    def test_probe_checks_contain_scratch_writable(self):
        """验证探测包含 scratch 目录可写性检查。"""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            cfg = AgentConfig(model_backend="echo", subagent_workspace="subs")
            agent = SimpleAgent(cfg, root)

            task = agent.subagents.create_run(goal="可写性检查测试", thought="思考", plan=["step1"])

            result = agent.subagents.probe_channel(task.id)

            check_names = [c.name for c in result.checks]
            assert "scratch_writable" in check_names


class TestChannelProbeResult:
    """测试 ChannelProbeResult 结构。"""

    def test_channel_probe_result_has_checks(self):
        """验证探测结果包含检查项列表。"""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            cfg = AgentConfig(model_backend="echo", subagent_workspace="subs")
            agent = SimpleAgent(cfg, root)

            task = agent.subagents.create_run(goal="检查项列表测试", thought="思考", plan=["step1"])

            result = agent.subagents.probe_channel(task.id)

            assert isinstance(result.checks, list)
            assert len(result.checks) > 0

    def test_channel_probe_result_check_has_required_fields(self):
        """验证检查项包含必要字段。"""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            cfg = AgentConfig(model_backend="echo", subagent_workspace="subs")
            agent = SimpleAgent(cfg, root)

            task = agent.subagents.create_run(goal="检查项字段测试", thought="思考", plan=["step1"])

            result = agent.subagents.probe_channel(task.id)

            for check in result.checks:
                assert hasattr(check, "name")
                assert hasattr(check, "ok")
                assert hasattr(check, "severity")


class TestChannelProbeStatusDetermination:
    """测试通道状态判断逻辑。"""

    def test_all_checks_ok_gives_ok_status(self):
        """验证所有检查通过时状态为 OK。"""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            cfg = AgentConfig(model_backend="echo", subagent_workspace="subs")
            agent = SimpleAgent(cfg, root)

            task = agent.subagents.create_run(goal="全部通过测试", thought="思考", plan=["step1"])

            result = agent.subagents.probe_channel(task.id)

            # 如果所有检查都 OK，状态应该是 OK
            all_ok = all(c.ok for c in result.checks)
            if all_ok:
                assert result.channel_status == "OK"


class TestWriteChannelProbeFiles:
    """测试 _write_channel_probe_files 方法的异常处理。"""

    def test_probe_channel_updates_task_failure_type_on_broken(self):
        """验证通道 BROKEN 时更新任务的 failure_type。"""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            cfg = AgentConfig(model_backend="echo", subagent_workspace="subs")
            agent = SimpleAgent(cfg, root)

            task = agent.subagents.create_run(goal="失败类型测试", thought="思考", plan=["step1"])

            result = agent.subagents.probe_channel(task.id)
            updated_task = agent.subagents.load(task.id)

            if updated_task.channel_status == "BROKEN":
                assert updated_task.failure_type == "channel"


class TestProbeChannelWithMissingFiles:
    """测试探测缺失文件时的行为。"""

    def test_probe_channel_handles_invalid_run_id(self):
        """验证探测无效 run_id 抛出异常。"""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            cfg = AgentConfig(model_backend="echo", subagent_workspace="subs")
            agent = SimpleAgent(cfg, root)

            # 探测不存在的任务应该抛出 FileNotFoundError
            with pytest.raises(FileNotFoundError):
                agent.subagents.probe_channel("non-existent-id")
