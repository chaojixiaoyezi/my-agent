"""验收辅助函数测试 - acceptance_helpers.py 验收辅助函数、检查清单、结果汇总。"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest


class TestBuildReadinessFindings:
    """测试 _build_readiness_findings 函数。"""

    def test_ready_for_acceptance_true(self, tmp_path: Path):
        """任务处于等待验收状态。"""
        from agent_py_agent.agent.subagents.acceptance_helpers import _build_readiness_findings

        task = MagicMock()
        task.status = "AWAITING_ACCEPTANCE"
        task.verification_status = "NEEDS_ACCEPTANCE"
        task.channel_status = "OK"
        task.runner_result_json = str(tmp_path / "runner.json")
        task.channel_probe_file = str(tmp_path / "probe.json")

        runner = {"structured_output_found": False}

        findings = _build_readiness_findings(task, runner, 1234567890.0)

        ready_finding = next((f for f in findings if f.name == "ready_for_acceptance"), None)
        assert ready_finding.ok is True

    def test_ready_for_acceptance_false(self, tmp_path: Path):
        """任务未处于等待验收状态。"""
        from agent_py_agent.agent.subagents.acceptance_helpers import _build_readiness_findings

        task = MagicMock()
        task.status = "RUNNING"
        task.verification_status = "UNVERIFIED"
        task.channel_status = "OK"
        task.runner_result_json = str(tmp_path / "runner.json")
        task.channel_probe_file = str(tmp_path / "probe.json")

        runner = {"structured_output_found": False}

        findings = _build_readiness_findings(task, runner, 1234567890.0)

        ready_finding = next((f for f in findings if f.name == "ready_for_acceptance"), None)
        assert ready_finding.ok is False

    def test_channel_not_broken(self, tmp_path: Path):
        """通道未损坏。"""
        from agent_py_agent.agent.subagents.acceptance_helpers import _build_readiness_findings

        task = MagicMock()
        task.status = "AWAITING_ACCEPTANCE"
        task.verification_status = "NEEDS_ACCEPTANCE"
        task.channel_status = "OK"
        task.runner_result_json = str(tmp_path / "runner.json")
        task.channel_probe_file = str(tmp_path / "probe.json")

        runner = {}

        findings = _build_readiness_findings(task, runner, 1234567890.0)

        channel_finding = next((f for f in findings if f.name == "channel_not_broken"), None)
        assert channel_finding.ok is True

    def test_channel_broken(self, tmp_path: Path):
        """通道损坏。"""
        from agent_py_agent.agent.subagents.acceptance_helpers import _build_readiness_findings

        task = MagicMock()
        task.status = "AWAITING_ACCEPTANCE"
        task.verification_status = "NEEDS_ACCEPTANCE"
        task.channel_status = "BROKEN"
        task.runner_result_json = str(tmp_path / "runner.json")
        task.channel_probe_file = str(tmp_path / "probe.json")

        runner = {}

        findings = _build_readiness_findings(task, runner, 1234567890.0)

        channel_finding = next((f for f in findings if f.name == "channel_not_broken"), None)
        assert channel_finding.ok is False

    def test_structured_output_ok(self, tmp_path: Path):
        """runner 结构化输出正常。"""
        from agent_py_agent.agent.subagents.acceptance_helpers import _build_readiness_findings

        task = MagicMock()
        task.status = "AWAITING_ACCEPTANCE"
        task.verification_status = "NEEDS_ACCEPTANCE"
        task.channel_status = "OK"
        task.runner_result_json = str(tmp_path / "runner.json")
        task.channel_probe_file = str(tmp_path / "probe.json")

        runner = {
            "structured_output_found": True,
            "structured_output_ok": True,
        }

        findings = _build_readiness_findings(task, runner, 1234567890.0)

        structured_finding = next((f for f in findings if f.name == "structured_output"), None)
        assert structured_finding.ok is True

    def test_structured_output_not_found(self, tmp_path: Path):
        """runner 未记录结构化输出。"""
        from agent_py_agent.agent.subagents.acceptance_helpers import _build_readiness_findings

        task = MagicMock()
        task.status = "AWAITING_ACCEPTANCE"
        task.verification_status = "NEEDS_ACCEPTANCE"
        task.channel_status = "OK"
        task.runner_result_json = str(tmp_path / "runner.json")
        task.channel_probe_file = str(tmp_path / "probe.json")

        runner = {
            "structured_output_found": False,
        }

        findings = _build_readiness_findings(task, runner, 1234567890.0)

        structured_finding = next((f for f in findings if f.name == "structured_output"), None)
        assert structured_finding.ok is True  # 未记录不算失败


class TestBuildEvidenceFindings:
    """测试 _build_evidence_findings 函数。"""

    def test_evidence_present(self, tmp_path: Path):
        """有可用验收证据。"""
        from agent_py_agent.agent.subagents.acceptance_helpers import _build_evidence_findings

        task = MagicMock()
        task.evidence = [MagicMock(ok=True), MagicMock(ok=True)]
        task.acceptance_checks = []
        task.used_tools = []
        task.acceptance_file = str(tmp_path / "acceptance.json")

        findings = _build_evidence_findings(task, 1234567890.0)

        evidence_finding = next((f for f in findings if f.name == "evidence_present"), None)
        assert evidence_finding.ok is True

    def test_evidence_not_present(self, tmp_path: Path):
        """缺少验收证据。"""
        from agent_py_agent.agent.subagents.acceptance_helpers import _build_evidence_findings

        task = MagicMock()
        task.evidence = []
        task.acceptance_checks = []
        task.used_tools = []
        task.acceptance_file = str(tmp_path / "acceptance.json")

        findings = _build_evidence_findings(task, 1234567890.0)

        evidence_finding = next((f for f in findings if f.name == "evidence_present"), None)
        assert evidence_finding.ok is False

    def test_evidence_not_failed(self, tmp_path: Path):
        """没有失败证据。"""
        from agent_py_agent.agent.subagents.acceptance_helpers import _build_evidence_findings

        task = MagicMock()
        task.evidence = [MagicMock(ok=True)]
        task.acceptance_checks = []
        task.used_tools = []
        task.acceptance_file = str(tmp_path / "acceptance.json")

        findings = _build_evidence_findings(task, 1234567890.0)

        not_failed_finding = next((f for f in findings if f.name == "evidence_not_failed"), None)
        assert not_failed_finding.ok is True

    def test_has_failed_evidence(self, tmp_path: Path):
        """存在失败证据。"""
        from agent_py_agent.agent.subagents.acceptance_helpers import _build_evidence_findings

        task = MagicMock()
        task.evidence = [MagicMock(ok=True), MagicMock(ok=False)]
        task.acceptance_checks = []
        task.used_tools = []
        task.acceptance_file = str(tmp_path / "acceptance.json")

        findings = _build_evidence_findings(task, 1234567890.0)

        not_failed_finding = next((f for f in findings if f.name == "evidence_not_failed"), None)
        assert not_failed_finding.ok is False

    def test_acceptance_requires_read_file_with_evidence(self, tmp_path: Path):
        """acceptance_checks 要求 read_file 且有证据。"""
        from agent_py_agent.agent.subagents.acceptance_helpers import _build_evidence_findings

        task = MagicMock()
        task.evidence = [
            MagicMock(
                ok=True,
                kind="read_file",
                command="read_file /path",
                summary="read file",
            )
        ]
        task.acceptance_checks = ["read_file 验证"]
        task.used_tools = ["read_file"]
        task.acceptance_file = str(tmp_path / "acceptance.json")

        findings = _build_evidence_findings(task, 1234567890.0)

        read_finding = next((f for f in findings if f.name == "acceptance_requires_read_file"), None)
        assert read_finding is not None
        assert read_finding.ok is True

    def test_acceptance_requires_read_file_without_evidence(self, tmp_path: Path):
        """acceptance_checks 要求 read_file 但缺少证据。"""
        from agent_py_agent.agent.subagents.acceptance_helpers import _build_evidence_findings

        task = MagicMock()
        task.evidence = []
        task.acceptance_checks = ["read_file 验证"]
        task.used_tools = ["read_file"]
        task.acceptance_file = str(tmp_path / "acceptance.json")

        findings = _build_evidence_findings(task, 1234567890.0)

        read_finding = next((f for f in findings if f.name == "acceptance_requires_read_file"), None)
        assert read_finding.ok is False

    def test_acceptance_requires_write_file_with_evidence(self, tmp_path: Path):
        """acceptance_checks 要求 write_file 且有证据。"""
        from agent_py_agent.agent.subagents.acceptance_helpers import _build_evidence_findings

        task = MagicMock()
        task.evidence = [
            MagicMock(
                ok=True,
                kind="write_file",
                command="write_file /path",
                summary="写入文件",
            )
        ]
        task.acceptance_checks = ["write_file 验证"]
        task.used_tools = ["write_file"]
        task.acceptance_file = str(tmp_path / "acceptance.json")

        findings = _build_evidence_findings(task, 1234567890.0)

        write_finding = next((f for f in findings if f.name == "acceptance_requires_write_file"), None)
        assert write_finding is not None
        assert write_finding.ok is True


class TestBuildOutputAndCapabilityFindings:
    """测试 _build_output_and_capability_findings 函数。"""

    def test_no_open_capability_requests(self, tmp_path: Path):
        """没有待处理 capability 请求。"""
        from agent_py_agent.agent.subagents.acceptance_helpers import (
            _build_output_and_capability_findings,
        )

        task = MagicMock()
        task.capability_requests = []
        task.capability_gaps = []

        output = {}

        findings = _build_output_and_capability_findings(task, output, 1234567890.0, lambda x: True)

        request_finding = next((f for f in findings if f.name == "no_open_capability_requests"), None)
        assert request_finding.ok is True

    def test_has_open_capability_requests(self, tmp_path: Path):
        """有待处理 capability 请求。"""
        from agent_py_agent.agent.subagents.acceptance_helpers import (
            _build_output_and_capability_findings,
        )

        task = MagicMock()
        task.capability_requests = [MagicMock(status="OPEN")]
        task.capability_gaps = []

        output = {}

        findings = _build_output_and_capability_findings(task, output, 1234567890.0, lambda x: True)

        request_finding = next((f for f in findings if f.name == "no_open_capability_requests"), None)
        assert request_finding.ok is False

    def test_no_output_blockers(self, tmp_path: Path):
        """output.json 没有 blocker。"""
        from agent_py_agent.agent.subagents.acceptance_helpers import (
            _build_output_and_capability_findings,
        )

        task = MagicMock()
        task.capability_requests = []
        task.capability_gaps = []

        output = {"blockers": []}

        findings = _build_output_and_capability_findings(task, output, 1234567890.0, lambda x: True)

        blocker_finding = next((f for f in findings if f.name == "no_output_blockers"), None)
        assert blocker_finding.ok is True

    def test_has_output_blockers(self, tmp_path: Path):
        """output.json 有 blocker。"""
        from agent_py_agent.agent.subagents.acceptance_helpers import (
            _build_output_and_capability_findings,
        )

        task = MagicMock()
        task.capability_requests = []
        task.capability_gaps = []

        output = {"blockers": ["配置错误"]}

        findings = _build_output_and_capability_findings(task, output, 1234567890.0, lambda x: True)

        blocker_finding = next((f for f in findings if f.name == "no_output_blockers"), None)
        assert blocker_finding.ok is False

    def test_tests_passed(self, tmp_path: Path):
        """所有测试通过。"""
        from agent_py_agent.agent.subagents.acceptance_helpers import (
            _build_output_and_capability_findings,
        )

        task = MagicMock()
        task.capability_requests = []
        task.capability_gaps = []

        output = {"tests": [{"ok": True}, {"ok": True}]}

        findings = _build_output_and_capability_findings(task, output, 1234567890.0, lambda x: True)

        test_finding = next((f for f in findings if f.name == "tests_passed"), None)
        assert test_finding.ok is True

    def test_has_failed_tests(self, tmp_path: Path):
        """有失败的测试。"""
        from agent_py_agent.agent.subagents.acceptance_helpers import (
            _build_output_and_capability_findings,
        )

        task = MagicMock()
        task.capability_requests = []
        task.capability_gaps = []

        output = {"tests": [{"ok": True}, {"ok": False}]}

        findings = _build_output_and_capability_findings(task, output, 1234567890.0, lambda x: True)

        test_finding = next((f for f in findings if f.name == "tests_passed"), None)
        assert test_finding.ok is False

    def test_artifact_paths_exist(self, tmp_path: Path):
        """artifact 路径存在。"""
        from agent_py_agent.agent.subagents.acceptance_helpers import (
            _build_output_and_capability_findings,
        )

        task = MagicMock()
        task.capability_requests = []
        task.capability_gaps = []

        output = {"artifacts": [{"path": "/tmp/test.txt"}]}

        findings = _build_output_and_capability_findings(task, output, 1234567890.0, lambda x: True)

        artifact_finding = next((f for f in findings if f.name == "artifact_paths_exist"), None)
        assert artifact_finding.ok is True

    def test_missing_artifact_paths(self, tmp_path: Path):
        """artifact 路径不存在。"""
        from agent_py_agent.agent.subagents.acceptance_helpers import (
            _build_output_and_capability_findings,
        )

        task = MagicMock()
        task.capability_requests = []
        task.capability_gaps = []

        output = {"artifacts": [{"path": "/tmp/missing.txt"}]}

        findings = _build_output_and_capability_findings(task, output, 1234567890.0, lambda x: False)

        artifact_finding = next((f for f in findings if f.name == "artifact_paths_exist"), None)
        assert artifact_finding.ok is False


class TestCheckArtifactExists:
    """测试 _check_artifact_exists 函数。"""

    def test_empty_path_returns_true(self, tmp_path: Path):
        """空路径返回 True。"""
        from agent_py_agent.agent.subagents.acceptance_helpers import _check_artifact_exists

        result = _check_artifact_exists(tmp_path, str(tmp_path), "")
        assert result is True

    def test_url_returns_true(self, tmp_path: Path):
        """URL 返回 True。"""
        from agent_py_agent.agent.subagents.acceptance_helpers import _check_artifact_exists

        result = _check_artifact_exists(tmp_path, str(tmp_path), "https://example.com/file.txt")
        assert result is True

    def test_absolute_path_exists(self, tmp_path: Path):
        """绝对路径存在时返回 True。"""
        from agent_py_agent.agent.subagents.acceptance_helpers import _check_artifact_exists

        test_file = tmp_path / "test.txt"
        test_file.write_text("content")

        result = _check_artifact_exists(tmp_path, str(tmp_path), str(test_file))
        assert result is True

    def test_relative_path_in_task_dir(self, tmp_path: Path):
        """任务目录中的相对路径。"""
        from agent_py_agent.agent.subagents.acceptance_helpers import _check_artifact_exists

        task_dir = tmp_path / "task_dir"
        task_dir.mkdir()
        test_file = task_dir / "file.txt"
        test_file.write_text("content")

        result = _check_artifact_exists(tmp_path, str(task_dir), "file.txt")
        assert result is True

    def test_relative_path_in_workspace(self, tmp_path: Path):
        """工作区中的相对路径。"""
        from agent_py_agent.agent.subagents.acceptance_helpers import _check_artifact_exists

        test_file = tmp_path / "workspace_file.txt"
        test_file.write_text("content")

        result = _check_artifact_exists(tmp_path, str(tmp_path / "task"), "workspace_file.txt")
        assert result is True

    def test_nested_relative_path_by_workspace_suffix(self, tmp_path: Path):
        """runner 少写外层目录时，按 workspace 内路径后缀找到 artifact。"""
        from agent_py_agent.agent.subagents.acceptance_helpers import _check_artifact_exists

        test_file = tmp_path / "real_run" / "grandchild_sorting_edge" / "sorting_edge_report.md"
        test_file.parent.mkdir(parents=True)
        test_file.write_text("ok", encoding="utf-8")

        result = _check_artifact_exists(
            tmp_path,
            str(tmp_path / "task"),
            "grandchild_sorting_edge/sorting_edge_report.md",
        )

        assert result is True

    def test_nonexistent_path_returns_false(self, tmp_path: Path):
        """不存在的路径返回 False。"""
        from agent_py_agent.agent.subagents.acceptance_helpers import _check_artifact_exists

        result = _check_artifact_exists(tmp_path, str(tmp_path), "nonexistent_file.txt")
        assert result is False
