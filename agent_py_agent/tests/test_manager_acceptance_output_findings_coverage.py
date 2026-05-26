from __future__ import annotations

import json
import time
from pathlib import Path
from unittest.mock import MagicMock

from agent_py_agent.tests.test_manager_acceptance_findings import (
    _FindingAssertMixin,
    _FindingReportMixin,
    _FindingSetupMixin,
)
from agent_py_agent.tests.test_manager_acceptance_output_findings import (
    _coverage_descendant_records,
    _coverage_task_attributes,
    _write_child_task_json,
)


class TestSubAgentAcceptanceCoverageFindings(_FindingSetupMixin, _FindingAssertMixin, _FindingReportMixin):
    """Coverage-record descendant health tests."""

    def test_parent_descendant_health_accepts_verified_coverage_record(self, tmp_path: Path):
        """坏 leaf 被 verified sibling 结构化覆盖时，父级后代健康门不再误挡。"""
        from agent_py_agent.agent.subagents.manager_acceptance_findings import (
            SubAgentAcceptanceFindingMixin,
        )

        class MockManager(SubAgentAcceptanceFindingMixin):
            def __init__(self):
                self.workspace = tmp_path

        manager = MockManager()
        for run_id, record in _coverage_descendant_records().items():
            _write_child_task_json(tmp_path, run_id, record)
        task = self._make_findings_task(
            tmp_path,
            goal="汇总越南市场监管、痛点和进入策略。",
            role="coordinator",
            child_ids=["bad-leaf", "good-leaf"],
        )
        task.attributes = _coverage_task_attributes()
        output = {"artifacts": [], "structured_output": {"status": "COMPLETED"}}
        self._write_findings_files(tmp_path, output)
        manager.validate_work_order = MagicMock(return_value=MagicMock(ok=True, missing=[]))

        findings = manager._acceptance_findings(task, output, {}, time.time())

        self._assert_finding(findings, "descendant_health", expected_ok=True)


class TestSubAgentAcceptanceInheritedRoleCoverageFindings(
    _FindingSetupMixin,
    _FindingAssertMixin,
    _FindingReportMixin,
):
    """Regression tests for inherited QA role wording."""

    def test_inherited_parent_qa_contract_does_not_bind_intermediate_coordinator(self, tmp_path: Path):
        """中间 coordinator 继承父级 QA 目标时，不应被硬性要求自己补齐全部 QA。"""
        from agent_py_agent.agent.subagents.manager_acceptance_findings import (
            SubAgentAcceptanceFindingMixin,
        )

        class MockManager(SubAgentAcceptanceFindingMixin):
            def __init__(self):
                self.workspace = tmp_path

        manager = MockManager()
        (tmp_path / "leaf").mkdir()
        (tmp_path / "leaf" / "task.json").write_text(
            json.dumps({"id": "leaf", "role": "leaf_worker", "child_ids": []}),
            encoding="utf-8",
        )
        task = self._make_findings_task(
            tmp_path,
            goal=(
                "你是页面协调，负责创建 leaf_worker 写页面。\n"
                "继承父级目标/边界（仅用于目录/权限/验收，不代表当前子任务要执行父级全部目标）：\n"
                "父级层级/协作约束：\n"
                "- 同时创建 tester（小傻妞-测试员）和 bug_finder（小傻妞-找茬员）\n"
                "- 至少一个 acceptor 给最终验收建议"
            ),
            role="child_coordinator",
            child_ids=["leaf"],
        )
        output = {"artifacts": [], "structured_output": {"status": "COMPLETED"}}
        self._write_findings_files(tmp_path, output)
        manager.validate_work_order = MagicMock(return_value=MagicMock(ok=True, missing=[]))

        findings = manager._acceptance_findings(task, output, {}, time.time())

        self._assert_finding(findings, "required_role_coverage", expected_ok=True)



class TestSubAgentAcceptanceTestsAndArtifactFindings(_FindingSetupMixin, _FindingAssertMixin, _FindingReportMixin):
    """Tests and artifact acceptance tests."""

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

    # LLM: Parent acceptance should validate artifact quality through the shared artifact contract.
    # 函数用途: output.json 上报坏 JSON 产物时，验收不能只看路径存在，还要给出 artifact_acceptance 失败。
    def test_artifact_acceptance_blocks_invalid_artifact_content(self, tmp_path: Path):
        from agent_py_agent.agent.subagents.manager_acceptance_findings import (
            SubAgentAcceptanceFindingMixin,
        )

        class MockManager(SubAgentAcceptanceFindingMixin):
            def __init__(self):
                self.workspace = tmp_path

        manager = MockManager()
        bad_json = tmp_path / "broken.json"
        bad_json.write_text("{bad", encoding="utf-8")
        task = self._make_findings_task(tmp_path)
        self._write_findings_files(tmp_path)
        manager.validate_work_order = MagicMock(return_value=MagicMock(ok=True, missing=[]))

        findings = manager._acceptance_findings(task, {"artifacts": [{"path": str(bad_json)}]}, {}, time.time())

        self._assert_finding(findings, "artifact_paths_exist", expected_ok=True)
        self._assert_finding(findings, "artifact_acceptance_passed", expected_ok=False)

    # LLM: This regression prevents internal runner final_report.md from satisfying user deliverables.
    # 函数用途: 验收必须看到 product root 下的 final_report.md，不能只接受 agent-run 内部交接报告。
    def test_required_product_file_must_exist_under_product_root(self, tmp_path: Path):
        from agent_py_agent.agent.subagents.manager_acceptance_findings import (
            SubAgentAcceptanceFindingMixin,
        )

        class MockManager(SubAgentAcceptanceFindingMixin):
            def __init__(self):
                self.workspace = tmp_path / ".my_agent_subagents"

        manager = MockManager()
        task_dir = tmp_path / ".my_agent_subagents" / "run-1"
        product_root = tmp_path / "product"
        internal_report = task_dir / "tasks" / "run-1" / "agents" / "run-1" / "final_report.md"
        internal_report.parent.mkdir(parents=True)
        internal_report.write_text("internal report", encoding="utf-8")
        task = self._make_findings_task(
            tmp_path,
            goal="需要交付最终报告。",
            task_dir=task_dir,
            allowed_write_roots=[str(task_dir), str(product_root)],
            attributes={"required_files": ["final_report.md"]},
        )
        self._write_findings_files(tmp_path)
        manager.validate_work_order = MagicMock(return_value=MagicMock(ok=True, missing=[]))
        manager._artifact_exists = MagicMock(return_value=True)

        findings = manager._acceptance_findings(
            task,
            {"artifacts": [{"path": str(internal_report)}]},
            {},
            time.time(),
        )

        self._assert_finding(findings, "artifact_paths_exist", expected_ok=True)
        self._assert_finding(findings, "required_product_files_exist", expected_ok=False)

        product_root.mkdir()
        (product_root / "final_report.md").write_text("user-facing report", encoding="utf-8")
        findings = manager._acceptance_findings(
            task,
            {"artifacts": [{"path": str(product_root / "final_report.md")}]},
            {},
            time.time(),
        )

        self._assert_finding(findings, "required_product_files_exist", expected_ok=True)

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
