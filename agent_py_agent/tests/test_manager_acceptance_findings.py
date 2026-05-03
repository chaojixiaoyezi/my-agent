"""验收发现测试 - manager_acceptance_findings.py 验收发现、问题分类、严重程度判断。"""
from __future__ import annotations

import json
import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest


class TestAcceptanceReviewFinding:
    """测试 AcceptanceReviewFinding 数据类。"""

    def test_finding_basic_creation(self, tmp_path: Path):
        """基本创建验收发现。"""
        from agent_py_agent.agent.subagents.reports import AcceptanceReviewFinding

        finding = AcceptanceReviewFinding(
            name="test_finding",
            ok=True,
            severity="P1",
            message="测试消息",
            evidence_path="/path/to/evidence",
            created_at=1234567890.0,
        )

        assert finding.name == "test_finding"
        assert finding.ok is True
        assert finding.severity == "P1"

    def test_finding_to_dict(self, tmp_path: Path):
        """验收发现转换为字典。"""
        from agent_py_agent.agent.subagents.reports import AcceptanceReviewFinding

        finding = AcceptanceReviewFinding(
            name="test",
            ok=False,
            severity="P0",
            message="测试",
            evidence_path="/test",
            created_at=1234567890.0,
        )

        from dataclasses import asdict

        data = asdict(finding)
        assert data["name"] == "test"
        assert data["ok"] is False
        assert data["severity"] == "P0"


class TestSubAgentAcceptanceFindingMixin:
    """测试 _acceptance_findings 方法。"""

    def test_work_order_validation(self, tmp_path: Path):
        """工单现场验证。"""
        from agent_py_agent.agent.subagents.manager_acceptance_findings import SubAgentAcceptanceFindingMixin

        class MockManager(SubAgentAcceptanceFindingMixin):
            def __init__(self):
                self.workspace = tmp_path

        manager = MockManager()

        task = MagicMock()
        task.id = "test"
        task.status = "AWAITING_ACCEPTANCE"
        task.verification_status = "NEEDS_ACCEPTANCE"
        task.output_json = str(tmp_path / "output.json")
        task.runner_result_json = str(tmp_path / "runner.json")
        task.evidence = []
        task.task_dir = str(tmp_path / "task_dir")
        task.channel_status = "OK"
        task.channel_probe_file = str(tmp_path / "probe.json")
        task.acceptance_file = str(tmp_path / "acceptance.json")
        task.capability_requests = []
        task.capability_gaps = []
        task.acceptance_checks = []
        task.used_tools = []

        (tmp_path / "output.json").write_text(json.dumps({}), encoding="utf-8")
        (tmp_path / "runner.json").write_text(json.dumps({}), encoding="utf-8")

        manager.validate_work_order = MagicMock(return_value=MagicMock(ok=True, missing=[]))

        findings = manager._acceptance_findings(task, {}, {}, time.time())

        # 第一个 finding 应该是 work_order
        assert findings[0].name == "work_order"
        assert findings[0].ok is True

    def test_ready_for_acceptance_check(self, tmp_path: Path):
        """验收就绪状态检查。"""
        from agent_py_agent.agent.subagents.manager_acceptance_findings import SubAgentAcceptanceFindingMixin

        class MockManager(SubAgentAcceptanceFindingMixin):
            def __init__(self):
                self.workspace = tmp_path

        manager = MockManager()

        task = MagicMock()
        task.id = "test"
        task.status = "AWAITING_ACCEPTANCE"
        task.verification_status = "NEEDS_ACCEPTANCE"
        task.output_json = str(tmp_path / "output.json")
        task.runner_result_json = str(tmp_path / "runner.json")
        task.evidence = []
        task.task_dir = str(tmp_path / "task_dir")
        task.channel_status = "OK"
        task.channel_probe_file = str(tmp_path / "probe.json")
        task.acceptance_file = str(tmp_path / "acceptance.json")
        task.capability_requests = []
        task.capability_gaps = []
        task.acceptance_checks = []
        task.used_tools = []

        (tmp_path / "output.json").write_text(json.dumps({}), encoding="utf-8")
        (tmp_path / "runner.json").write_text(json.dumps({}), encoding="utf-8")
        (tmp_path / "probe.json").write_text("{}", encoding="utf-8")
        (tmp_path / "acceptance.json").write_text("[]", encoding="utf-8")

        manager.validate_work_order = MagicMock(return_value=MagicMock(ok=True, missing=[]))

        findings = manager._acceptance_findings(task, {}, {}, time.time())

        ready_finding = next((f for f in findings if f.name == "ready_for_acceptance"), None)
        assert ready_finding is not None
        assert ready_finding.ok is True

    def test_channel_not_broken(self, tmp_path: Path):
        """通道未损坏检查。"""
        from agent_py_agent.agent.subagents.manager_acceptance_findings import SubAgentAcceptanceFindingMixin

        class MockManager(SubAgentAcceptanceFindingMixin):
            def __init__(self):
                self.workspace = tmp_path

        manager = MockManager()

        task = MagicMock()
        task.id = "test"
        task.status = "AWAITING_ACCEPTANCE"
        task.verification_status = "NEEDS_ACCEPTANCE"
        task.output_json = str(tmp_path / "output.json")
        task.runner_result_json = str(tmp_path / "runner.json")
        task.evidence = []
        task.task_dir = str(tmp_path / "task_dir")
        task.channel_status = "OK"
        task.channel_probe_file = str(tmp_path / "probe.json")
        task.acceptance_file = str(tmp_path / "acceptance.json")
        task.capability_requests = []
        task.capability_gaps = []
        task.acceptance_checks = []
        task.used_tools = []

        (tmp_path / "output.json").write_text(json.dumps({}), encoding="utf-8")
        (tmp_path / "runner.json").write_text(json.dumps({}), encoding="utf-8")
        (tmp_path / "probe.json").write_text("{}", encoding="utf-8")
        (tmp_path / "acceptance.json").write_text("[]", encoding="utf-8")

        manager.validate_work_order = MagicMock(return_value=MagicMock(ok=True, missing=[]))

        findings = manager._acceptance_findings(task, {}, {}, time.time())

        channel_finding = next((f for f in findings if f.name == "channel_not_broken"), None)
        assert channel_finding.ok is True

    def test_channel_broken_blocks(self, tmp_path: Path):
        """通道损坏阻止验收。"""
        from agent_py_agent.agent.subagents.manager_acceptance_findings import SubAgentAcceptanceFindingMixin

        class MockManager(SubAgentAcceptanceFindingMixin):
            def __init__(self):
                self.workspace = tmp_path

        manager = MockManager()

        task = MagicMock()
        task.id = "test"
        task.status = "AWAITING_ACCEPTANCE"
        task.verification_status = "NEEDS_ACCEPTANCE"
        task.output_json = str(tmp_path / "output.json")
        task.runner_result_json = str(tmp_path / "runner.json")
        task.evidence = []
        task.task_dir = str(tmp_path / "task_dir")
        task.channel_status = "BROKEN"  # 通道损坏
        task.channel_probe_file = str(tmp_path / "probe.json")
        task.acceptance_file = str(tmp_path / "acceptance.json")
        task.capability_requests = []
        task.capability_gaps = []
        task.acceptance_checks = []
        task.used_tools = []

        (tmp_path / "output.json").write_text(json.dumps({}), encoding="utf-8")
        (tmp_path / "runner.json").write_text(json.dumps({}), encoding="utf-8")
        (tmp_path / "probe.json").write_text("{}", encoding="utf-8")
        (tmp_path / "acceptance.json").write_text("[]", encoding="utf-8")

        manager.validate_work_order = MagicMock(return_value=MagicMock(ok=True, missing=[]))

        findings = manager._acceptance_findings(task, {}, {}, time.time())

        channel_finding = next((f for f in findings if f.name == "channel_not_broken"), None)
        assert channel_finding.ok is False

    def test_evidence_present_check(self, tmp_path: Path):
        """验收证据存在检查。"""
        from agent_py_agent.agent.subagents.manager_acceptance_findings import SubAgentAcceptanceFindingMixin

        class MockManager(SubAgentAcceptanceFindingMixin):
            def __init__(self):
                self.workspace = tmp_path

        manager = MockManager()

        task = MagicMock()
        task.id = "test"
        task.status = "AWAITING_ACCEPTANCE"
        task.verification_status = "NEEDS_ACCEPTANCE"
        task.output_json = str(tmp_path / "output.json")
        task.runner_result_json = str(tmp_path / "runner.json")
        task.evidence = [MagicMock(ok=True), MagicMock(ok=True)]  # 有可用证据
        task.task_dir = str(tmp_path / "task_dir")
        task.channel_status = "OK"
        task.channel_probe_file = str(tmp_path / "probe.json")
        task.acceptance_file = str(tmp_path / "acceptance.json")
        task.capability_requests = []
        task.capability_gaps = []
        task.acceptance_checks = []
        task.used_tools = []

        (tmp_path / "output.json").write_text(json.dumps({}), encoding="utf-8")
        (tmp_path / "runner.json").write_text(json.dumps({}), encoding="utf-8")
        (tmp_path / "probe.json").write_text("{}", encoding="utf-8")
        (tmp_path / "acceptance.json").write_text("[]", encoding="utf-8")

        manager.validate_work_order = MagicMock(return_value=MagicMock(ok=True, missing=[]))

        findings = manager._acceptance_findings(task, {}, {}, time.time())

        evidence_finding = next((f for f in findings if f.name == "evidence_present"), None)
        assert evidence_finding.ok is True

    def test_evidence_not_failed(self, tmp_path: Path):
        """没有失败证据检查。"""
        from agent_py_agent.agent.subagents.manager_acceptance_findings import SubAgentAcceptanceFindingMixin

        class MockManager(SubAgentAcceptanceFindingMixin):
            def __init__(self):
                self.workspace = tmp_path

        manager = MockManager()

        task = MagicMock()
        task.id = "test"
        task.status = "AWAITING_ACCEPTANCE"
        task.verification_status = "NEEDS_ACCEPTANCE"
        task.output_json = str(tmp_path / "output.json")
        task.runner_result_json = str(tmp_path / "runner.json")
        task.evidence = [MagicMock(ok=True), MagicMock(ok=False)]  # 有一个失败的证据
        task.task_dir = str(tmp_path / "task_dir")
        task.channel_status = "OK"
        task.channel_probe_file = str(tmp_path / "probe.json")
        task.acceptance_file = str(tmp_path / "acceptance.json")
        task.capability_requests = []
        task.capability_gaps = []
        task.acceptance_checks = []
        task.used_tools = []

        (tmp_path / "output.json").write_text(json.dumps({}), encoding="utf-8")
        (tmp_path / "runner.json").write_text(json.dumps({}), encoding="utf-8")
        (tmp_path / "probe.json").write_text("{}", encoding="utf-8")
        (tmp_path / "acceptance.json").write_text("[]", encoding="utf-8")

        manager.validate_work_order = MagicMock(return_value=MagicMock(ok=True, missing=[]))

        findings = manager._acceptance_findings(task, {}, {}, time.time())

        failed_finding = next((f for f in findings if f.name == "evidence_not_failed"), None)
        assert failed_finding.ok is False

    def test_no_open_capability_requests(self, tmp_path: Path):
        """没有待处理 capability 请求检查。"""
        from agent_py_agent.agent.subagents.manager_acceptance_findings import SubAgentAcceptanceFindingMixin

        class MockManager(SubAgentAcceptanceFindingMixin):
            def __init__(self):
                self.workspace = tmp_path

        manager = MockManager()

        task = MagicMock()
        task.id = "test"
        task.status = "AWAITING_ACCEPTANCE"
        task.verification_status = "NEEDS_ACCEPTANCE"
        task.output_json = str(tmp_path / "output.json")
        task.runner_result_json = str(tmp_path / "runner.json")
        task.evidence = []
        task.task_dir = str(tmp_path / "task_dir")
        task.channel_status = "OK"
        task.channel_probe_file = str(tmp_path / "probe.json")
        task.acceptance_file = str(tmp_path / "acceptance.json")
        task.capability_requests = []  # 没有 OPEN 请求
        task.capability_gaps = []
        task.acceptance_checks = []
        task.used_tools = []

        (tmp_path / "output.json").write_text(json.dumps({}), encoding="utf-8")
        (tmp_path / "runner.json").write_text(json.dumps({}), encoding="utf-8")
        (tmp_path / "probe.json").write_text("{}", encoding="utf-8")
        (tmp_path / "acceptance.json").write_text("[]", encoding="utf-8")

        manager.validate_work_order = MagicMock(return_value=MagicMock(ok=True, missing=[]))

        findings = manager._acceptance_findings(task, {}, {}, time.time())

        request_finding = next((f for f in findings if f.name == "no_open_capability_requests"), None)
        assert request_finding.ok is True

    def test_no_output_blockers(self, tmp_path: Path):
        """output.json 没有 blocker 检查。"""
        from agent_py_agent.agent.subagents.manager_acceptance_findings import SubAgentAcceptanceFindingMixin

        class MockManager(SubAgentAcceptanceFindingMixin):
            def __init__(self):
                self.workspace = tmp_path

        manager = MockManager()

        task = MagicMock()
        task.id = "test"
        task.status = "AWAITING_ACCEPTANCE"
        task.verification_status = "NEEDS_ACCEPTANCE"
        task.output_json = str(tmp_path / "output.json")
        task.runner_result_json = str(tmp_path / "runner.json")
        task.evidence = []
        task.task_dir = str(tmp_path / "task_dir")
        task.channel_status = "OK"
        task.channel_probe_file = str(tmp_path / "probe.json")
        task.acceptance_file = str(tmp_path / "acceptance.json")
        task.capability_requests = []
        task.capability_gaps = []
        task.acceptance_checks = []
        task.used_tools = []

        output_data = {"blockers": []}  # 没有 blocker
        (tmp_path / "output.json").write_text(json.dumps(output_data), encoding="utf-8")
        (tmp_path / "runner.json").write_text(json.dumps({}), encoding="utf-8")
        (tmp_path / "probe.json").write_text("{}", encoding="utf-8")
        (tmp_path / "acceptance.json").write_text("[]", encoding="utf-8")

        manager.validate_work_order = MagicMock(return_value=MagicMock(ok=True, missing=[]))

        findings = manager._acceptance_findings(task, output_data, {}, time.time())

        blocker_finding = next((f for f in findings if f.name == "no_output_blockers"), None)
        assert blocker_finding.ok is True

    def test_tests_passed_check(self, tmp_path: Path):
        """测试通过检查。"""
        from agent_py_agent.agent.subagents.manager_acceptance_findings import SubAgentAcceptanceFindingMixin

        class MockManager(SubAgentAcceptanceFindingMixin):
            def __init__(self):
                self.workspace = tmp_path

        manager = MockManager()

        task = MagicMock()
        task.id = "test"
        task.status = "AWAITING_ACCEPTANCE"
        task.verification_status = "NEEDS_ACCEPTANCE"
        task.output_json = str(tmp_path / "output.json")
        task.runner_result_json = str(tmp_path / "runner.json")
        task.evidence = []
        task.task_dir = str(tmp_path / "task_dir")
        task.channel_status = "OK"
        task.channel_probe_file = str(tmp_path / "probe.json")
        task.acceptance_file = str(tmp_path / "acceptance.json")
        task.capability_requests = []
        task.capability_gaps = []
        task.acceptance_checks = []
        task.used_tools = []

        output_data = {"tests": [{"ok": True}, {"ok": True}]}  # 所有测试通过
        (tmp_path / "output.json").write_text(json.dumps(output_data), encoding="utf-8")
        (tmp_path / "runner.json").write_text(json.dumps({}), encoding="utf-8")
        (tmp_path / "probe.json").write_text("{}", encoding="utf-8")
        (tmp_path / "acceptance.json").write_text("[]", encoding="utf-8")

        manager.validate_work_order = MagicMock(return_value=MagicMock(ok=True, missing=[]))

        findings = manager._acceptance_findings(task, output_data, {}, time.time())

        test_finding = next((f for f in findings if f.name == "tests_passed"), None)
        assert test_finding.ok is True

    def test_artifact_paths_exist(self, tmp_path: Path):
        """artifact 路径存在检查。"""
        from agent_py_agent.agent.subagents.manager_acceptance_findings import SubAgentAcceptanceFindingMixin

        class MockManager(SubAgentAcceptanceFindingMixin):
            def __init__(self):
                self.workspace = tmp_path

        manager = MockManager()

        task = MagicMock()
        task.id = "test"
        task.status = "AWAITING_ACCEPTANCE"
        task.verification_status = "NEEDS_ACCEPTANCE"
        task.output_json = str(tmp_path / "output.json")
        task.runner_result_json = str(tmp_path / "runner.json")
        task.evidence = []
        task.task_dir = str(tmp_path / "task_dir")
        task.channel_status = "OK"
        task.channel_probe_file = str(tmp_path / "probe.json")
        task.acceptance_file = str(tmp_path / "acceptance.json")
        task.capability_requests = []
        task.capability_gaps = []
        task.acceptance_checks = []
        task.used_tools = []

        output_data = {"artifacts": [{"path": "/tmp/exists.txt"}]}
        (tmp_path / "output.json").write_text(json.dumps(output_data), encoding="utf-8")
        (tmp_path / "runner.json").write_text(json.dumps({}), encoding="utf-8")
        (tmp_path / "probe.json").write_text("{}", encoding="utf-8")
        (tmp_path / "acceptance.json").write_text("[]", encoding="utf-8")

        manager.validate_work_order = MagicMock(return_value=MagicMock(ok=True, missing=[]))
        manager._artifact_exists = MagicMock(return_value=True)

        findings = manager._acceptance_findings(task, output_data, {}, time.time())

        artifact_finding = next((f for f in findings if f.name == "artifact_paths_exist"), None)
        assert artifact_finding.ok is True


class TestSeverityLevels:
    """测试严重程度级别。"""

    def test_p0_blocks_acceptance(self, tmp_path: Path):
        """P0 严重程度阻止验收。"""
        from agent_py_agent.agent.subagents.manager_acceptance_findings import SubAgentAcceptanceFindingMixin

        class MockManager(SubAgentAcceptanceFindingMixin):
            def __init__(self):
                self.workspace = tmp_path

        manager = MockManager()

        task = MagicMock()
        task.id = "test"
        task.status = "AWAITING_ACCEPTANCE"
        task.verification_status = "NEEDS_ACCEPTANCE"
        task.output_json = str(tmp_path / "output.json")
        task.runner_result_json = str(tmp_path / "runner.json")
        task.evidence = []  # 没有证据，P0 问题
        task.task_dir = str(tmp_path / "task_dir")
        task.channel_status = "OK"
        task.channel_probe_file = str(tmp_path / "probe.json")
        task.acceptance_file = str(tmp_path / "acceptance.json")
        task.capability_requests = []
        task.capability_gaps = []
        task.acceptance_checks = []
        task.used_tools = []

        (tmp_path / "output.json").write_text(json.dumps({}), encoding="utf-8")
        (tmp_path / "runner.json").write_text(json.dumps({}), encoding="utf-8")
        (tmp_path / "probe.json").write_text("{}", encoding="utf-8")
        (tmp_path / "acceptance.json").write_text("[]", encoding="utf-8")

        manager.validate_work_order = MagicMock(return_value=MagicMock(ok=True, missing=[]))

        findings = manager._acceptance_findings(task, {}, {}, time.time())

        # 验收应该失败，因为 evidence_present P0 为 False
        ok = all(item.ok or item.severity == "P2" for item in findings)
        assert ok is False

    def test_p1_warns_but_not_blocks(self, tmp_path: Path):
        """P1 严重程度警告但不阻止。"""
        from agent_py_agent.agent.subagents.manager_acceptance_findings import SubAgentAcceptanceFindingMixin

        class MockManager(SubAgentAcceptanceFindingMixin):
            def __init__(self):
                self.workspace = tmp_path

        manager = MockManager()

        task = MagicMock()
        task.id = "test"
        task.status = "AWAITING_ACCEPTANCE"
        task.verification_status = "NEEDS_ACCEPTANCE"
        task.output_json = str(tmp_path / "output.json")
        task.runner_result_json = str(tmp_path / "runner.json")
        task.evidence = [MagicMock(ok=True)]
        task.task_dir = str(tmp_path / "task_dir")
        task.channel_status = "OK"
        task.channel_probe_file = str(tmp_path / "probe.json")
        task.acceptance_file = str(tmp_path / "acceptance.json")
        task.capability_requests = []  # 空则 no_open_capability_requests 通过
        task.capability_gaps = []  # 空则 no_open_capability_gaps 通过
        task.acceptance_checks = []
        task.used_tools = []

        (tmp_path / "output.json").write_text(json.dumps({}), encoding="utf-8")
        (tmp_path / "runner.json").write_text(json.dumps({}), encoding="utf-8")
        (tmp_path / "probe.json").write_text("{}", encoding="utf-8")
        (tmp_path / "acceptance.json").write_text("[]", encoding="utf-8")

        manager.validate_work_order = MagicMock(return_value=MagicMock(ok=True, missing=[]))

        findings = manager._acceptance_findings(task, {}, {}, time.time())

        # 验证 P1 severity 的存在（no_open_capability_gaps 是 P1）
        p1_findings = [f for f in findings if f.severity == "P1"]
        assert len(p1_findings) > 0  # 至少有 P1 severity 的 finding