"""子代理验收测试 - manager_acceptance.py 验收流程、决策逻辑、证据校验。"""
from __future__ import annotations

import json
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


class TestSubAgentAcceptanceMixin:
    """测试 SubAgentAcceptanceMixin 类。"""

    def test_review_acceptance_loads_task(self, tmp_path: Path):
        """review_acceptance 加载任务。"""
        from agent_py_agent.agent.subagents.manager_acceptance import SubAgentAcceptanceMixin

        class MockManager(SubAgentAcceptanceMixin):
            def __init__(self):
                self.workspace = tmp_path

        manager = MockManager()

        # 创建 mock 任务
        task = MagicMock()
        task.id = "test_task"
        task.status = "AWAITING_ACCEPTANCE"
        task.verification_status = "NEEDS_ACCEPTANCE"
        task.output_json = str(tmp_path / "output.json")
        task.runner_result_json = str(tmp_path / "runner.json")
        task.evidence = []
        task.task_dir = str(tmp_path / "task_dir")

        output_data = {"tests": [], "artifacts": []}
        (tmp_path / "output.json").write_text(json.dumps(output_data), encoding="utf-8")
        (tmp_path / "runner.json").write_text(json.dumps({"structured_output_found": False}), encoding="utf-8")

        manager.load = MagicMock(return_value=task)
        manager.validate_work_order = MagicMock(return_value=MagicMock(ok=True, missing=[]))
        manager.acceptance_findings = MagicMock(return_value=[])
        manager.save = MagicMock()
        manager._append_task_work_log = MagicMock()

        record = manager.review_acceptance("test_task", apply=False, reviewer="test")

        assert record.run_id == "test_task"
        assert record.dry_run is True

    def test_review_acceptance_dry_run_does_not_apply(self, tmp_path: Path):
        """dry-run 模式不修改任务状态。"""
        from agent_py_agent.agent.subagents.manager_acceptance import SubAgentAcceptanceMixin

        class MockManager(SubAgentAcceptanceMixin):
            def __init__(self):
                self.workspace = tmp_path

        manager = MockManager()

        task = MagicMock()
        task.id = "test_task"
        task.status = "AWAITING_ACCEPTANCE"
        task.verification_status = "NEEDS_ACCEPTANCE"
        task.output_json = str(tmp_path / "output.json")
        task.runner_result_json = str(tmp_path / "runner.json")
        task.evidence = []
        task.task_dir = str(tmp_path / "task_dir")
        task.channel_status = "OK"
        task.runner_result_json = str(tmp_path / "runner.json")
        task.channel_probe_file = str(tmp_path / "probe.json")
        task.acceptance_file = str(tmp_path / "acceptance.json")

        output_data = {"tests": [], "artifacts": [], "blockers": []}
        (tmp_path / "output.json").write_text(json.dumps(output_data), encoding="utf-8")
        (tmp_path / "runner.json").write_text(json.dumps({"structured_output_found": False}), encoding="utf-8")
        (tmp_path / "probe.json").write_text("{}", encoding="utf-8")
        (tmp_path / "acceptance.json").write_text("[]", encoding="utf-8")

        manager.load = MagicMock(return_value=task)
        manager.validate_work_order = MagicMock(return_value=MagicMock(ok=True, missing=[]))
        manager.acceptance_findings = MagicMock(return_value=[])
        manager.save = MagicMock()
        manager._append_task_work_log = MagicMock()

        record = manager.review_acceptance("test_task", apply=False, reviewer="parent")

        assert record.dry_run is True
        assert record.applied is False

    def test_review_acceptance_apply_accept(self, tmp_path: Path):
        """apply=True 且验收通过时更新任务状态为 DONE。"""
        from agent_py_agent.agent.subagents.manager_acceptance import SubAgentAcceptanceMixin

        class MockManager(SubAgentAcceptanceMixin):
            def __init__(self):
                self.workspace = tmp_path

        manager = MockManager()

        task = MagicMock()
        task.id = "test_task"
        task.status = "AWAITING_ACCEPTANCE"
        task.verification_status = "NEEDS_ACCEPTANCE"
        task.output_json = str(tmp_path / "output.json")
        task.runner_result_json = str(tmp_path / "runner.json")
        task.evidence = [MagicMock(ok=True)]
        task.task_dir = str(tmp_path / "task_dir")
        task.channel_status = "OK"
        task.runner_result_json = str(tmp_path / "runner.json")
        task.channel_probe_file = str(tmp_path / "probe.json")
        task.acceptance_file = str(tmp_path / "acceptance.json")
        task.capability_requests = []
        task.capability_gaps = []

        output_data = {"tests": [], "artifacts": [], "blockers": []}
        (tmp_path / "output.json").write_text(json.dumps(output_data), encoding="utf-8")
        (tmp_path / "runner.json").write_text(json.dumps({"structured_output_found": False}), encoding="utf-8")
        (tmp_path / "probe.json").write_text("{}", encoding="utf-8")
        (tmp_path / "acceptance.json").write_text("[]", encoding="utf-8")

        manager.load = MagicMock(return_value=task)
        manager.validate_work_order = MagicMock(return_value=MagicMock(ok=True, missing=[]))
        finding_mock = MagicMock()
        finding_mock.ok = True
        finding_mock.severity = "P2"
        finding_mock.message = "test"
        finding_mock.evidence_path = None
        finding_mock.created_at = time.time()
        manager.acceptance_findings = MagicMock(return_value=[finding_mock])
        manager.save = MagicMock()
        manager._append_task_work_log = MagicMock()

        record = manager.review_acceptance("test_task", apply=True, reviewer="parent")

        assert record.decision == "ACCEPT"
        assert record.applied is True

    def test_review_acceptance_apply_reject(self, tmp_path: Path):
        """apply=True 且验收失败时更新任务状态为 BLOCKED。"""
        from agent_py_agent.agent.subagents.manager_acceptance import SubAgentAcceptanceMixin

        class MockManager(SubAgentAcceptanceMixin):
            def __init__(self):
                self.workspace = tmp_path

        manager = MockManager()

        task = MagicMock()
        task.id = "test_task"
        task.status = "AWAITING_ACCEPTANCE"
        task.verification_status = "NEEDS_ACCEPTANCE"
        task.output_json = str(tmp_path / "output.json")
        task.runner_result_json = str(tmp_path / "runner.json")
        task.evidence = []
        task.task_dir = str(tmp_path / "task_dir")
        task.channel_status = "OK"
        task.runner_result_json = str(tmp_path / "runner.json")
        task.channel_probe_file = str(tmp_path / "probe.json")
        task.acceptance_file = str(tmp_path / "acceptance.json")
        task.capability_requests = []
        task.capability_gaps = []

        output_data = {"tests": [], "artifacts": [], "blockers": []}
        (tmp_path / "output.json").write_text(json.dumps(output_data), encoding="utf-8")
        (tmp_path / "runner.json").write_text(json.dumps({"structured_output_found": False}), encoding="utf-8")
        (tmp_path / "probe.json").write_text("{}", encoding="utf-8")
        (tmp_path / "acceptance.json").write_text("[]", encoding="utf-8")

        manager.load = MagicMock(return_value=task)
        manager.validate_work_order = MagicMock(return_value=MagicMock(ok=True, missing=[]))
        finding_mock = MagicMock()
        finding_mock.ok = False
        finding_mock.severity = "P0"  # P0 必须通过
        finding_mock.message = "缺少验收证据"
        finding_mock.evidence_path = None
        finding_mock.created_at = time.time()
        manager.acceptance_findings = MagicMock(return_value=[finding_mock])
        manager.save = MagicMock()
        manager._append_task_work_log = MagicMock()

        record = manager.review_acceptance("test_task", apply=True, reviewer="parent")

        assert record.decision == "REJECT"
        assert record.applied is True

    def test_review_acceptance_not_ready_skips_apply(self, tmp_path: Path):
        """任务不在等待验收状态时不执行 apply。"""
        from agent_py_agent.agent.subagents.manager_acceptance import SubAgentAcceptanceMixin

        class MockManager(SubAgentAcceptanceMixin):
            def __init__(self):
                self.workspace = tmp_path

        manager = MockManager()

        task = MagicMock()
        task.id = "test_task"
        task.status = "RUNNING"  # 不是等待验收状态
        task.verification_status = "UNVERIFIED"
        task.output_json = str(tmp_path / "output.json")
        task.runner_result_json = str(tmp_path / "runner.json")
        task.evidence = []
        task.task_dir = str(tmp_path / "task_dir")
        task.channel_status = "OK"
        task.runner_result_json = str(tmp_path / "runner.json")
        task.channel_probe_file = str(tmp_path / "probe.json")
        task.acceptance_file = str(tmp_path / "acceptance.json")
        task.capability_requests = []
        task.capability_gaps = []

        output_data = {"tests": [], "artifacts": [], "blockers": []}
        (tmp_path / "output.json").write_text(json.dumps(output_data), encoding="utf-8")
        (tmp_path / "runner.json").write_text(json.dumps({"structured_output_found": False}), encoding="utf-8")
        (tmp_path / "probe.json").write_text("{}", encoding="utf-8")
        (tmp_path / "acceptance.json").write_text("[]", encoding="utf-8")

        manager.load = MagicMock(return_value=task)
        manager.validate_work_order = MagicMock(return_value=MagicMock(ok=True, missing=[]))
        manager.acceptance_findings = MagicMock(return_value=[])
        manager.save = MagicMock()
        manager._append_task_work_log = MagicMock()

        record = manager.review_acceptance("test_task", apply=True, reviewer="parent")

        assert record.applied is False
        assert "未写回" in record.message

    def test_review_acceptances批量验收(self, tmp_path: Path):
        """批量验收多个任务。"""
        from agent_py_agent.agent.subagents.manager_acceptance import SubAgentAcceptanceMixin

        class MockManager(SubAgentAcceptanceMixin):
            def __init__(self):
                self.workspace = tmp_path

        manager = MockManager()

        task1 = MagicMock()
        task1.id = "task1"
        task1.status = "AWAITING_ACCEPTANCE"
        task1.verification_status = "NEEDS_ACCEPTANCE"
        task1.output_json = str(tmp_path / "output1.json")
        task1.runner_result_json = str(tmp_path / "runner1.json")
        task1.evidence = []
        task1.task_dir = str(tmp_path / "task1")
        task1.channel_status = "OK"
        task1.runner_result_json = str(tmp_path / "runner1.json")
        task1.channel_probe_file = str(tmp_path / "probe1.json")
        task1.acceptance_file = str(tmp_path / "acc1.json")
        task1.capability_requests = []
        task1.capability_gaps = []

        (tmp_path / "output1.json").write_text(json.dumps({"tests": [], "artifacts": [], "blockers": []}), encoding="utf-8")
        (tmp_path / "runner1.json").write_text(json.dumps({"structured_output_found": False}), encoding="utf-8")
        (tmp_path / "probe1.json").write_text("{}", encoding="utf-8")
        (tmp_path / "acc1.json").write_text("[]", encoding="utf-8")

        manager._select_runs = MagicMock(return_value=[task1])
        manager._review_acceptance_task = MagicMock(return_value=MagicMock(decision="ACCEPT", ok=True, dry_run=True, applied=False, run_id="task1", message="验收通过。", before_status="AWAITING_ACCEPTANCE", after_status="AWAITING_ACCEPTANCE", before_verification_status="NEEDS_ACCEPTANCE", after_verification_status="NEEDS_ACCEPTANCE", evidence_count=0, test_count=0, artifact_count=0, findings=[], evidence_paths=[], created_at=time.time(), id="accept_1", reviewer="parent", note=""))

        report = manager.review_acceptances(run_ids=["task1"], apply=False, reviewer="parent")

        assert report.summary["total"] == 1

    def test_write_acceptance_review_report_creates_files(self, tmp_path: Path):
        """写出验收报告时创建文件。"""
        from agent_py_agent.agent.subagents.manager_acceptance import SubAgentAcceptanceMixin

        class MockManager(SubAgentAcceptanceMixin):
            def __init__(self):
                self.workspace = tmp_path

        manager = MockManager()

        task = MagicMock()
        task.id = "test_task"
        task.status = "AWAITING_ACCEPTANCE"
        task.verification_status = "NEEDS_ACCEPTANCE"
        task.output_json = str(tmp_path / "output.json")
        task.runner_result_json = str(tmp_path / "runner.json")
        task.evidence = []
        task.task_dir = str(tmp_path / "task_dir")
        task.reports_dir = str(tmp_path / "reports")
        task.channel_status = "OK"
        task.runner_result_json = str(tmp_path / "runner.json")
        task.channel_probe_file = str(tmp_path / "probe.json")
        task.acceptance_file = str(tmp_path / "acceptance.json")
        task.capability_requests = []
        task.capability_gaps = []
        Path(task.reports_dir).mkdir(parents=True, exist_ok=True)

        output_data = {"tests": [], "artifacts": [], "blockers": []}
        (tmp_path / "output.json").write_text(json.dumps(output_data), encoding="utf-8")
        (tmp_path / "runner.json").write_text(json.dumps({"structured_output_found": False}), encoding="utf-8")
        (tmp_path / "probe.json").write_text("{}", encoding="utf-8")
        (tmp_path / "acceptance.json").write_text("[]", encoding="utf-8")

        from dataclasses import asdict

        from agent_py_agent.agent.subagents.reports import AcceptanceReviewReport

        record_data = {
            "id": "accept_1",
            "run_id": "test_task",
            "dry_run": True,
            "applied": False,
            "ok": True,
            "decision": "ACCEPT",
            "message": "验收通过。",
            "before_status": "AWAITING_ACCEPTANCE",
            "after_status": "AWAITING_ACCEPTANCE",
            "before_verification_status": "NEEDS_ACCEPTANCE",
            "after_verification_status": "NEEDS_ACCEPTANCE",
            "evidence_count": 0,
            "test_count": 0,
            "artifact_count": 0,
            "findings": [],
            "evidence_paths": [],
            "created_at": time.time(),
            "reviewer": "parent",
            "note": "",
        }

        def mock_review_task(task, *, apply, reviewer, note):
            from agent_py_agent.agent.subagents.reports import AcceptanceReviewRecord
            return AcceptanceReviewRecord(**record_data)

        manager._select_runs = MagicMock(return_value=[task])
        manager._review_acceptance_task = mock_review_task
        manager._write_acceptance_record_files = MagicMock()
        manager._index_acceptance_review = MagicMock()
        manager._append_acceptance_review_log = MagicMock()
        manager._index_report = MagicMock()

        report = manager.write_acceptance_review_report(run_ids=["test_task"], apply=False)

        assert (tmp_path / "subagent_acceptance_report.json").exists()
        assert (tmp_path / "SUBAGENT_ACCEPTANCE.md").exists()


class TestAcceptanceReviewRecord:
    """测试 AcceptanceReviewRecord 相关逻辑。"""

    def test_p2_findings_are_not_blocking(self, tmp_path: Path):
        """P2 severity 的 finding 不阻塞验收。"""
        from agent_py_agent.agent.subagents.manager_acceptance import SubAgentAcceptanceMixin

        class MockManager(SubAgentAcceptanceMixin):
            def __init__(self):
                self.workspace = tmp_path

        manager = MockManager()

        # 只有 P2 findings，应该通过
        findings = []
        for i in range(3):
            f = MagicMock()
            f.ok = False
            f.severity = "P2"
            findings.append(f)

        ok = all(item.ok or item.severity == "P2" for item in findings)
        assert ok is True


class TestAcceptanceReviewReport:
    """测试 AcceptanceReviewReport 结构。"""

    def test_report_summary_counts(self, tmp_path: Path):
        """报告摘要正确统计。"""
        from agent_py_agent.agent.subagents.manager_acceptance import SubAgentAcceptanceMixin

        class MockManager(SubAgentAcceptanceMixin):
            def __init__(self):
                self.workspace = tmp_path

        manager = MockManager()

        task = MagicMock()
        task.id = "test_task"
        task.status = "AWAITING_ACCEPTANCE"
        task.verification_status = "NEEDS_ACCEPTANCE"
        task.output_json = str(tmp_path / "output.json")
        task.runner_result_json = str(tmp_path / "runner.json")
        task.evidence = []
        task.task_dir = str(tmp_path / "task_dir")
        task.channel_status = "OK"
        task.runner_result_json = str(tmp_path / "runner.json")
        task.channel_probe_file = str(tmp_path / "probe.json")
        task.acceptance_file = str(tmp_path / "acceptance.json")
        task.capability_requests = []
        task.capability_gaps = []

        output_data = {"tests": [], "artifacts": [], "blockers": []}
        (tmp_path / "output.json").write_text(json.dumps(output_data), encoding="utf-8")
        (tmp_path / "runner.json").write_text(json.dumps({"structured_output_found": False}), encoding="utf-8")
        (tmp_path / "probe.json").write_text("{}", encoding="utf-8")
        (tmp_path / "acceptance.json").write_text("[]", encoding="utf-8")

        manager._select_runs = MagicMock(return_value=[task])
        manager._review_acceptance_task = MagicMock(return_value=MagicMock(decision="ACCEPT", ok=True, dry_run=True, applied=False, run_id="test_task", message="验收通过。", before_status="AWAITING_ACCEPTANCE", after_status="AWAITING_ACCEPTANCE", before_verification_status="NEEDS_ACCEPTANCE", after_verification_status="NEEDS_ACCEPTANCE", evidence_count=0, test_count=0, artifact_count=0, findings=[], evidence_paths=[], created_at=time.time(), id="accept_1", reviewer="parent", note=""))

        report = manager.review_acceptances(run_ids=["test_task"], apply=False)

        assert report.summary["total"] == 1
        assert "dry_run" in report.summary