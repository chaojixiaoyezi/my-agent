"""Tests for SubAgentDispatchMixin: dispatch records, parent planner reports, and log writing.

给人看的解释：
测试子代理调度模块：dispatch 记录生成、parent planner 报告、日志写入。
"""
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from agent_py_agent.agent.config import AgentConfig
from agent_py_agent.agent.core import SimpleAgent


class TestMakeDispatchRecord:
    """测试 make_dispatch_record 方法。"""

    def test_make_dispatch_record_basic(self):
        """验证基本 dispatch 记录生成。"""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            cfg = AgentConfig(model_backend="echo", subagent_workspace="subs")
            agent = SimpleAgent(cfg, root)

            record = agent.subagents.make_dispatch_record(
                step="runner_execute",
                action="execute",
                run_id="test-run-001",
                dry_run=False,
                applied=True,
                ok=True,
                message="任务执行成功",
            )

            assert record.step == "runner_execute"
            assert record.action == "execute"
            assert record.run_id == "test-run-001"
            assert record.dry_run is False
            assert record.applied is True
            assert record.ok is True
            assert record.message == "任务执行成功"
            assert record.id.startswith("dispatch-")

    def test_make_dispatch_record_with_status_changes(self):
        """验证带状态变化的 dispatch 记录。"""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            cfg = AgentConfig(model_backend="echo", subagent_workspace="subs")
            agent = SimpleAgent(cfg, root)

            record = agent.subagents.make_dispatch_record(
                step="status_update",
                action="update",
                run_id="test-run-002",
                before_status="PLANNING",
                after_status="RUNNING",
                before_verification_status="UNVERIFIED",
                after_verification_status="VERIFIED",
            )

            assert record.before_status == "PLANNING"
            assert record.after_status == "RUNNING"
            assert record.before_verification_status == "UNVERIFIED"
            assert record.after_verification_status == "VERIFIED"

    def test_make_dispatch_record_with_evidence_paths(self):
        """验证带证据路径的 dispatch 记录。"""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            cfg = AgentConfig(model_backend="echo", subagent_workspace="subs")
            agent = SimpleAgent(cfg, root)

            evidence = ["/path/to/evidence1.md", "/path/to/evidence2.json"]
            record = agent.subagents.make_dispatch_record(
                step="runner_result",
                action="accept",
                run_id="test-run-003",
                evidence_paths=evidence,
            )

            assert record.evidence_paths == evidence

    def test_make_dispatch_record_dry_run_default(self):
        """验证 dry_run 默认为 True。"""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            cfg = AgentConfig(model_backend="echo", subagent_workspace="subs")
            agent = SimpleAgent(cfg, root)

            record = agent.subagents.make_dispatch_record(
                step="test",
                action="test",
            )

            assert record.dry_run is True
            assert record.applied is False


class TestBuildDispatchReport:
    """测试 build_dispatch_report 方法。"""

    def test_build_dispatch_report_empty(self):
        """验证空记录列表生成报告。"""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            cfg = AgentConfig(model_backend="echo", subagent_workspace="subs")
            agent = SimpleAgent(cfg, root)

            report = agent.subagents.build_dispatch_report([], dry_run=True)

            assert report.dry_run is True
            assert report.summary["total"] == 0
            assert len(report.records) == 0

    def test_build_dispatch_report_with_records(self):
        """验证带多条记录的报告生成。"""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            cfg = AgentConfig(model_backend="echo", subagent_workspace="subs")
            agent = SimpleAgent(cfg, root)

            records = [
                agent.subagents.make_dispatch_record(
                    step="runner_execute",
                    action="execute",
                    run_id="run-001",
                    ok=True,
                ),
                agent.subagents.make_dispatch_record(
                    step="runner_execute",
                    action="execute",
                    run_id="run-002",
                    ok=False,
                ),
                agent.subagents.make_dispatch_record(
                    step="runner_result",
                    action="accept",
                    run_id="run-001",
                    ok=True,
                ),
            ]

            report = agent.subagents.build_dispatch_report(records, dry_run=False)

            assert report.dry_run is False
            assert report.summary["total"] == 3
            assert report.summary["runner_execute"] == 2
            assert report.summary["runner_result"] == 1
            assert report.summary["ok"] == 2
            assert report.summary["failed"] == 1

    def test_build_dispatch_report_counts_applied_vs_dry_run(self):
        """验证报告正确统计 applied 和 dry_run 数量。"""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            cfg = AgentConfig(model_backend="echo", subagent_workspace="subs")
            agent = SimpleAgent(cfg, root)

            records = [
                agent.subagents.make_dispatch_record(
                    step="test",
                    action="test",
                    applied=True,
                    dry_run=False,
                ),
                agent.subagents.make_dispatch_record(
                    step="test",
                    action="test",
                    applied=False,
                    dry_run=True,
                ),
            ]

            report = agent.subagents.build_dispatch_report(records, dry_run=False)

            assert report.summary["applied"] == 1
            assert report.summary["dry_run"] == 1


class TestMakeDispatchWatchRecord:
    """测试 make_dispatch_watch_record 方法。"""

    def test_make_dispatch_watch_record_basic(self):
        """验证基本 watch 记录生成。"""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            cfg = AgentConfig(model_backend="echo", subagent_workspace="subs")
            agent = SimpleAgent(cfg, root)

            record = agent.subagents.make_dispatch_watch_record(
                cycle=5,
                dry_run=False,
                ok=True,
                message="watch cycle completed",
                dispatch_record_count=3,
            )

            assert record.cycle == 5
            assert record.dry_run is False
            assert record.ok is True
            assert record.message == "watch cycle completed"
            assert record.dispatch_record_count == 3
            assert record.id.startswith("watch-")

    def test_make_dispatch_watch_record_with_summary(self):
        """验证带 dispatch summary 的 watch 记录。"""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            cfg = AgentConfig(model_backend="echo", subagent_workspace="subs")
            agent = SimpleAgent(cfg, root)

            summary = {"execute": 2, "accept": 1, "ok": 3}
            record = agent.subagents.make_dispatch_watch_record(
                cycle=1,
                dry_run=True,
                ok=True,
                message="test",
                dispatch_summary=summary,
                dispatch_record_count=0,
            )

            assert record.dispatch_summary == summary


class TestMakeParentPlannerRecord:
    """测试 make_parent_planner_record 方法。"""

    def test_make_parent_planner_record_basic(self):
        """验证基本 parent planner 记录生成。"""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            cfg = AgentConfig(model_backend="echo", subagent_workspace="subs")
            agent = SimpleAgent(cfg, root)

            record = agent.subagents.make_parent_planner_record(
                dry_run=False,
                triggered=True,
                ok=True,
                decision="EXECUTE",
                message="执行子代理",
            )

            assert record.dry_run is False
            assert record.triggered is True
            assert record.ok is True
            assert record.decision == "EXECUTE"
            assert record.id.startswith("planner-")

    def test_make_parent_planner_record_with_gate_summary(self):
        """验证带 gate summary 的 parent planner 记录。"""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            cfg = AgentConfig(model_backend="echo", subagent_workspace="subs")
            agent = SimpleAgent(cfg, root)

            gate_summary = {"has_active": 2, "has_pending": 1, "has_blocked": 0}
            record = agent.subagents.make_parent_planner_record(
                dry_run=True,
                triggered=True,
                ok=True,
                decision="PLAN",
                message="规划中",
                gate_summary=gate_summary,
            )

            assert record.gate_summary == gate_summary

    def test_make_parent_planner_record_with_parse_error(self):
        """验证带解析错误的 parent planner 记录。"""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            cfg = AgentConfig(model_backend="echo", subagent_workspace="subs")
            agent = SimpleAgent(cfg, root)

            record = agent.subagents.make_parent_planner_record(
                dry_run=False,
                triggered=True,
                ok=False,
                decision="PLAN",
                message="解析失败",
                parse_error="JSONDecodeError: Expecting value",
            )

            assert record.ok is False
            assert record.parse_error == "JSONDecodeError: Expecting value"


class TestBuildParentPlannerReport:
    """测试 build_parent_planner_report 方法。"""

    def test_build_parent_planner_report_empty(self):
        """验证空记录列表生成报告。"""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            cfg = AgentConfig(model_backend="echo", subagent_workspace="subs")
            agent = SimpleAgent(cfg, root)

            report = agent.subagents.build_parent_planner_report([], dry_run=True)

            assert report.dry_run is True
            assert report.summary["total"] == 0

    def test_build_parent_planner_report_with_records(self):
        """验证带多条记录的 parent planner 报告生成。"""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            cfg = AgentConfig(model_backend="echo", subagent_workspace="subs")
            agent = SimpleAgent(cfg, root)

            records = [
                agent.subagents.make_parent_planner_record(
                    dry_run=False,
                    triggered=True,
                    ok=True,
                    decision="EXECUTE",
                    message="执行",
                ),
                agent.subagents.make_parent_planner_record(
                    dry_run=False,
                    triggered=False,
                    ok=True,
                    decision="SKIP",
                    message="跳过",
                ),
            ]

            report = agent.subagents.build_parent_planner_report(records, dry_run=False)

            assert report.summary["total"] == 2
            assert report.summary["triggered"] == 1
            assert report.summary["skipped"] == 1
            assert report.summary["EXECUTE"] == 1
            assert report.summary["SKIP"] == 1


class TestWriteDispatchReport:
    """测试 write_dispatch_report 方法的异常场景。"""

    def test_write_dispatch_report_without_local_store(self):
        """验证没有 LocalStore 时仍能写入文件。"""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            cfg = AgentConfig(model_backend="echo", subagent_workspace="subs")
            agent = SimpleAgent(cfg, root)
            agent.subagents.local_store = None  # 禁用 LocalStore

            records = [
                agent.subagents.make_dispatch_record(
                    step="test",
                    action="test",
                    ok=True,
                ),
            ]
            report = agent.subagents.build_dispatch_report(records, dry_run=True)

            result = agent.subagents.write_dispatch_report(report)

            assert result.generated_at > 0
            # 文件应该已写入
            json_file = agent.subagents.workspace / "subagent_dispatch_report.json"
            md_file = agent.subagents.workspace / "SUBAGENT_DISPATCH.md"
            assert json_file.exists()
            assert md_file.exists()

    def test_write_parent_planner_report_without_local_store(self):
        """验证没有 LocalStore 时仍能写入 parent planner 报告。"""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            cfg = AgentConfig(model_backend="echo", subagent_workspace="subs")
            agent = SimpleAgent(cfg, root)
            agent.subagents.local_store = None  # 禁用 LocalStore

            records = [
                agent.subagents.make_parent_planner_record(
                    dry_run=True,
                    triggered=True,
                    ok=True,
                    decision="PLAN",
                    message="测试",
                ),
            ]
            report = agent.subagents.build_parent_planner_report(records, dry_run=True)

            result = agent.subagents.write_parent_planner_report(report)

            assert result.generated_at > 0
            json_file = agent.subagents.workspace / "parent_planner_report.json"
            md_file = agent.subagents.workspace / "PARENT_PLANNER.md"
            assert json_file.exists()
            assert md_file.exists()


class TestWriteParentPlannerExchange:
    """测试 write_parent_planner_exchange 方法。"""

    def test_write_parent_planner_exchange_with_response(self):
        """验证写出 prompt 和 response。"""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            cfg = AgentConfig(model_backend="echo", subagent_workspace="subs")
            agent = SimpleAgent(cfg, root)

            prompt = "分析任务并决定是否执行子代理"
            response = "决定执行 2 个子代理"

            prompt_path, response_path = agent.subagents.write_parent_planner_exchange(
                prompt, response
            )

            assert Path(prompt_path).exists()
            assert Path(response_path).exists()
            assert Path(prompt_path).read_text(encoding="utf-8") == prompt
            assert Path(response_path).read_text(encoding="utf-8") == response

    def test_write_parent_planner_exchange_without_response(self):
        """验证只有 prompt 没有 response 时也能工作。"""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            cfg = AgentConfig(model_backend="echo", subagent_workspace="subs")
            agent = SimpleAgent(cfg, root)

            prompt = "分析任务"

            prompt_path, response_path = agent.subagents.write_parent_planner_exchange(prompt)

            assert Path(prompt_path).exists()
            assert Path(prompt_path).read_text(encoding="utf-8") == prompt
            # response 文件不存在因为没有提供
