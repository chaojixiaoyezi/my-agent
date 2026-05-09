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


# ─── Findings Setup Mixin ───────────────────────────────────────────────────────


class _FindingSetupMixin:
    """Shared task mock factory for findings tests."""

    def _make_findings_task(self, tmp_path: Path, **overrides) -> MagicMock:
        """Create a standard task mock for acceptance findings tests."""
        task = MagicMock()
        task.id = "test"
        task.status = overrides.get("status", "AWAITING_ACCEPTANCE")
        task.verification_status = overrides.get("verification_status", "NEEDS_ACCEPTANCE")
        task.output_json = str(tmp_path / "output.json")
        task.runner_result_json = str(tmp_path / "runner.json")
        task.evidence = overrides.get("evidence", [])
        task.task_dir = str(tmp_path / "task_dir")
        task.channel_status = overrides.get("channel_status", "OK")
        task.channel_probe_file = str(tmp_path / "probe.json")
        task.acceptance_file = str(tmp_path / "acceptance.json")
        task.capability_requests = overrides.get("capability_requests", [])
        task.capability_gaps = overrides.get("capability_gaps", [])
        task.acceptance_checks = overrides.get("acceptance_checks", [])
        task.used_tools = overrides.get("used_tools", [])
        return task

    def _write_findings_files(self, tmp_path: Path, output_data: dict | None = None) -> None:
        """Write standard JSON files for findings tests."""
        (tmp_path / "output.json").write_text(json.dumps(output_data or {}), encoding="utf-8")
        (tmp_path / "runner.json").write_text(json.dumps({}), encoding="utf-8")
        (tmp_path / "probe.json").write_text("{}", encoding="utf-8")
        (tmp_path / "acceptance.json").write_text("[]", encoding="utf-8")

    def _call_findings(self, manager: MagicMock, task: MagicMock, tmp_path: Path, output_data: dict | None = None) -> list:
        """Call acceptance_findings with standard args."""
        if output_data is not None:
            return manager._acceptance_findings(task, output_data, {}, time.time())
        return manager.acceptance_findings(task, {}, {}, time.time())


# ─── Findings Assert Mixin ──────────────────────────────────────────────────────


class _FindingAssertMixin:
    """Shared assertion helpers for findings tests."""

    def _assert_finding(self, findings: list, name: str, expected_ok: bool) -> None:
        """Assert a finding by name has the expected ok status."""
        finding = next((f for f in findings if f.name == name), None)
        assert finding is not None, f"Finding '{name}' not found"
        assert finding.ok is expected_ok


# ─── Findings Report Mixin ──────────────────────────────────────────────────────


class _FindingReportMixin:
    """Report helpers (unused in current tests but reserved for future extension)."""

    pass


# ─── Main Test Class ────────────────────────────────────────────────────────────


class TestSubAgentAcceptanceFindingMixin(_FindingSetupMixin, _FindingAssertMixin, _FindingReportMixin):
    """测试 _acceptance_findings 方法。"""

    def test_work_order_validation(self, tmp_path: Path):
        """工单现场验证。"""
        from agent_py_agent.agent.subagents.manager_acceptance_findings import (
            SubAgentAcceptanceFindingMixin,
        )

        class MockManager(SubAgentAcceptanceFindingMixin):
            def __init__(self):
                self.workspace = tmp_path

        manager = MockManager()
        task = self._make_findings_task(tmp_path)
        self._write_findings_files(tmp_path)
        manager.validate_work_order = MagicMock(return_value=MagicMock(ok=True, missing=[]))

        findings = self._call_findings(manager, task, tmp_path)

        self._assert_finding(findings, "work_order", expected_ok=True)

    def test_ready_for_acceptance_check(self, tmp_path: Path):
        """验收就绪状态检查。"""
        from agent_py_agent.agent.subagents.manager_acceptance_findings import (
            SubAgentAcceptanceFindingMixin,
        )

        class MockManager(SubAgentAcceptanceFindingMixin):
            def __init__(self):
                self.workspace = tmp_path

        manager = MockManager()
        task = self._make_findings_task(tmp_path)
        self._write_findings_files(tmp_path)
        manager.validate_work_order = MagicMock(return_value=MagicMock(ok=True, missing=[]))

        findings = self._call_findings(manager, task, tmp_path)

        self._assert_finding(findings, "ready_for_acceptance", expected_ok=True)

    def test_channel_not_broken(self, tmp_path: Path):
        """通道未损坏检查。"""
        from agent_py_agent.agent.subagents.manager_acceptance_findings import (
            SubAgentAcceptanceFindingMixin,
        )

        class MockManager(SubAgentAcceptanceFindingMixin):
            def __init__(self):
                self.workspace = tmp_path

        manager = MockManager()
        task = self._make_findings_task(tmp_path)
        self._write_findings_files(tmp_path)
        manager.validate_work_order = MagicMock(return_value=MagicMock(ok=True, missing=[]))

        findings = self._call_findings(manager, task, tmp_path)

        self._assert_finding(findings, "channel_not_broken", expected_ok=True)

    def test_channel_broken_blocks(self, tmp_path: Path):
        """通道损坏阻止验收。"""
        from agent_py_agent.agent.subagents.manager_acceptance_findings import (
            SubAgentAcceptanceFindingMixin,
        )

        class MockManager(SubAgentAcceptanceFindingMixin):
            def __init__(self):
                self.workspace = tmp_path

        manager = MockManager()
        task = self._make_findings_task(tmp_path, channel_status="BROKEN")
        self._write_findings_files(tmp_path)
        manager.validate_work_order = MagicMock(return_value=MagicMock(ok=True, missing=[]))

        findings = self._call_findings(manager, task, tmp_path)

        self._assert_finding(findings, "channel_not_broken", expected_ok=False)

    def test_evidence_present_check(self, tmp_path: Path):
        """验收证据存在检查。"""
        from agent_py_agent.agent.subagents.manager_acceptance_findings import (
            SubAgentAcceptanceFindingMixin,
        )

        class MockManager(SubAgentAcceptanceFindingMixin):
            def __init__(self):
                self.workspace = tmp_path

        manager = MockManager()
        task = self._make_findings_task(tmp_path, evidence=[MagicMock(ok=True), MagicMock(ok=True)])
        self._write_findings_files(tmp_path)
        manager.validate_work_order = MagicMock(return_value=MagicMock(ok=True, missing=[]))

        findings = self._call_findings(manager, task, tmp_path)

        self._assert_finding(findings, "evidence_present", expected_ok=True)

    def test_evidence_not_failed(self, tmp_path: Path):
        """没有失败证据检查。"""
        from agent_py_agent.agent.subagents.manager_acceptance_findings import (
            SubAgentAcceptanceFindingMixin,
        )

        class MockManager(SubAgentAcceptanceFindingMixin):
            def __init__(self):
                self.workspace = tmp_path

        manager = MockManager()
        task = self._make_findings_task(tmp_path, evidence=[MagicMock(ok=True), MagicMock(ok=False)])
        self._write_findings_files(tmp_path)
        manager.validate_work_order = MagicMock(return_value=MagicMock(ok=True, missing=[]))

        findings = self._call_findings(manager, task, tmp_path)

        self._assert_finding(findings, "evidence_not_failed", expected_ok=False)

    def test_no_open_capability_requests(self, tmp_path: Path):
        """没有待处理 capability 请求检查。"""
        from agent_py_agent.agent.subagents.manager_acceptance_findings import (
            SubAgentAcceptanceFindingMixin,
        )

        class MockManager(SubAgentAcceptanceFindingMixin):
            def __init__(self):
                self.workspace = tmp_path

        manager = MockManager()
        task = self._make_findings_task(tmp_path)
        self._write_findings_files(tmp_path)
        manager.validate_work_order = MagicMock(return_value=MagicMock(ok=True, missing=[]))

        findings = self._call_findings(manager, task, tmp_path)

        self._assert_finding(findings, "no_open_capability_requests", expected_ok=True)





class TestSubAgentAcceptanceOutputFindingMixin(_FindingSetupMixin, _FindingAssertMixin, _FindingReportMixin):
    """测试 _acceptance_findings 方法。"""

    def test_no_output_blockers(self, tmp_path: Path):
        """output.json 没有 blocker 检查。"""
        from agent_py_agent.agent.subagents.manager_acceptance_findings import (
            SubAgentAcceptanceFindingMixin,
        )

        class MockManager(SubAgentAcceptanceFindingMixin):
            def __init__(self):
                self.workspace = tmp_path

        manager = MockManager()
        task = self._make_findings_task(tmp_path)
        self._write_findings_files(tmp_path)
        manager.validate_work_order = MagicMock(return_value=MagicMock(ok=True, missing=[]))

        findings = manager._acceptance_findings(task, {"blockers": []}, {}, time.time())

        self._assert_finding(findings, "no_output_blockers", expected_ok=True)

    def test_tests_passed_check(self, tmp_path: Path):
        """测试通过检查。"""
        from agent_py_agent.agent.subagents.manager_acceptance_findings import (
            SubAgentAcceptanceFindingMixin,
        )

        class MockManager(SubAgentAcceptanceFindingMixin):
            def __init__(self):
                self.workspace = tmp_path

        manager = MockManager()
        task = self._make_findings_task(tmp_path)
        self._write_findings_files(tmp_path)
        manager.validate_work_order = MagicMock(return_value=MagicMock(ok=True, missing=[]))

        findings = manager._acceptance_findings(task, {"tests": [{"ok": True}, {"ok": True}]}, {}, time.time())

        self._assert_finding(findings, "tests_passed", expected_ok=True)

    def test_artifact_paths_exist(self, tmp_path: Path):
        """artifact 路径存在检查。"""
        from agent_py_agent.agent.subagents.manager_acceptance_findings import (
            SubAgentAcceptanceFindingMixin,
        )

        class MockManager(SubAgentAcceptanceFindingMixin):
            def __init__(self):
                self.workspace = tmp_path

        manager = MockManager()
        task = self._make_findings_task(tmp_path)
        self._write_findings_files(tmp_path)
        manager.validate_work_order = MagicMock(return_value=MagicMock(ok=True, missing=[]))
        manager._artifact_exists = MagicMock(return_value=True)

        findings = manager._acceptance_findings(task, {"artifacts": [{"path": "/tmp/exists.txt"}]}, {}, time.time())

        self._assert_finding(findings, "artifact_paths_exist", expected_ok=True)

    def test_nested_relative_artifact_paths_exist(self, tmp_path: Path):
        """artifact 路径少写外层运行目录时，验收层仍能在工作区内核对。"""
        from agent_py_agent.agent.subagents.manager_acceptance_findings import (
            SubAgentAcceptanceFindingMixin,
        )

        class MockManager(SubAgentAcceptanceFindingMixin):
            def __init__(self):
                self.workspace = tmp_path / ".my_agent_subagents"

        manager = MockManager()
        manager.workspace.mkdir()
        test_file = tmp_path / "real_run" / "grandchild_sorting_edge" / "sorting_edge_report.md"
        test_file.parent.mkdir(parents=True)
        test_file.write_text("ok", encoding="utf-8")
        task = self._make_findings_task(tmp_path)
        self._write_findings_files(tmp_path)
        manager.validate_work_order = MagicMock(return_value=MagicMock(ok=True, missing=[]))

        findings = manager._acceptance_findings(
            task,
            {"artifacts": [{"path": "grandchild_sorting_edge/sorting_edge_report.md"}]},
            {},
            time.time(),
        )

        self._assert_finding(findings, "artifact_paths_exist", expected_ok=True)


class TestSeverityLevels:
    """测试严重程度级别。"""

    def test_p0_blocks_acceptance(self, tmp_path: Path):
        """P0 严重程度阻止验收。"""
        from agent_py_agent.agent.subagents.manager_acceptance_findings import (
            SubAgentAcceptanceFindingMixin,
        )

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

        findings = manager.acceptance_findings(task, {}, {}, time.time())

        # 验收应该失败，因为 evidence_present P0 为 False
        ok = all(item.ok or item.severity == "P2" for item in findings)
        assert ok is False

    def test_p1_warns_but_not_blocks(self, tmp_path: Path):
        """P1 严重程度警告但不阻止。"""
        from agent_py_agent.agent.subagents.manager_acceptance_findings import (
            SubAgentAcceptanceFindingMixin,
        )

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

        findings = manager.acceptance_findings(task, {}, {}, time.time())

        # 验证 P1 severity 的存在（no_open_capability_gaps 是 P1）
        p1_findings = [f for f in findings if f.severity == "P1"]
        assert len(p1_findings) > 0  # 至少有 P1 severity 的 finding
