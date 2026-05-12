"""Split acceptance finding tests for focused code-size boundaries."""
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


class TestSubAgentAcceptanceOutputMiscFindings(_FindingSetupMixin, _FindingAssertMixin, _FindingReportMixin):
    """Misc output and artifact acceptance tests."""

    def test_required_child_goal_without_child_ids_blocks_acceptance(self, tmp_path: Path):
        """任务要求创建下级时，不能只在结构化结果里伪造 child_run_ids。"""
        from agent_py_agent.agent.subagents.manager_acceptance_findings import (
            SubAgentAcceptanceFindingMixin,
        )

        class MockManager(SubAgentAcceptanceFindingMixin):
            def __init__(self):
                self.workspace = tmp_path

        manager = MockManager()
        task = self._make_findings_task(
            tmp_path,
            goal="你的职责：创建 depth=3 小小小傻妞-执行工人(leaf_worker)。",
            child_ids=[],
        )
        output = {"artifacts": [], "structured_output": {"status": "COMPLETED"}}
        self._write_findings_files(tmp_path, output)
        manager.validate_work_order = MagicMock(return_value=MagicMock(ok=True, missing=[]))

        findings = manager._acceptance_findings(task, output, {}, time.time())

        self._assert_finding(findings, "required_child_spawned", expected_ok=False)

    def test_leaf_self_creation_text_does_not_require_child_ids(self, tmp_path: Path):
        """leaf 的验收文案出现“创建 leaf_worker”时，不应误判它还要创建下级。"""
        from agent_py_agent.agent.subagents.manager_acceptance_findings import (
            SubAgentAcceptanceFindingMixin,
        )

        class MockManager(SubAgentAcceptanceFindingMixin):
            def __init__(self):
                self.workspace = tmp_path

        manager = MockManager()
        task = self._make_findings_task(
            tmp_path,
            goal="depth=3 leaf_worker。验收条件：真实创建 leaf_worker 并执行测试。",
            role="leaf_worker",
            child_ids=[],
        )
        output = {"artifacts": [], "structured_output": {"status": "COMPLETED"}}
        self._write_findings_files(tmp_path, output)
        manager.validate_work_order = MagicMock(return_value=MagicMock(ok=True, missing=[]))

        findings = manager._acceptance_findings(task, output, {}, time.time())

        self._assert_finding(findings, "required_child_spawned", expected_ok=True)

    def test_leaf_inherited_parent_depth_constraints_do_not_require_child_ids(self, tmp_path: Path):
        """leaf 继承父级 depth=3 约束时，不应被误判还要继续创建下级。"""
        from agent_py_agent.agent.subagents.manager_acceptance_findings import (
            SubAgentAcceptanceFindingMixin,
        )

        class MockManager(SubAgentAcceptanceFindingMixin):
            def __init__(self):
                self.workspace = tmp_path

        manager = MockManager()
        task = self._make_findings_task(
            tmp_path,
            goal=(
                "在 build 目录下创建 index.html 和 style.css。\n"
                "父级层级/协作约束：\n"
                "- 必须先创建 depth=3 worker 实际写文件\n"
                "- 禁止创建小小小小傻妞节点"
            ),
            role="leaf_worker",
            agent_name="小小小傻妞-首页写手",
            child_ids=[],
        )
        output = {"artifacts": [], "structured_output": {"status": "COMPLETED"}}
        self._write_findings_files(tmp_path, output)
        manager.validate_work_order = MagicMock(return_value=MagicMock(ok=True, missing=[]))

        findings = manager._acceptance_findings(task, output, {}, time.time())

        self._assert_finding(findings, "required_child_spawned", expected_ok=True)



class TestSubAgentAcceptanceRoleCoverageFindings(_FindingSetupMixin, _FindingAssertMixin, _FindingReportMixin):
    """QA role coverage acceptance tests."""

    def test_required_qa_roles_without_descendant_roles_blocks_acceptance(self, tmp_path: Path):
        """用户明确要求 tester/bug_finder/acceptor 时，不能只用普通 leaf_worker 冒充验收链路。"""
        from agent_py_agent.agent.subagents.manager_acceptance_findings import (
            SubAgentAcceptanceFindingMixin,
        )

        class MockManager(SubAgentAcceptanceFindingMixin):
            def __init__(self):
                self.workspace = tmp_path

        manager = MockManager()
        (tmp_path / "child").mkdir()
        (tmp_path / "child" / "task.json").write_text(
            json.dumps({"id": "child", "role": "leaf_worker", "child_ids": []}),
            encoding="utf-8",
        )
        task = self._make_findings_task(
            tmp_path,
            goal="必须至少覆盖 tester / bug_finder / acceptor 三类 QA 子代理。",
            role="coordinator",
            child_ids=["child"],
        )
        output = {"artifacts": [], "structured_output": {"status": "COMPLETED"}}
        self._write_findings_files(tmp_path, output)
        manager.validate_work_order = MagicMock(return_value=MagicMock(ok=True, missing=[]))

        findings = manager._acceptance_findings(task, output, {}, time.time())

        self._assert_finding(findings, "required_role_coverage", expected_ok=False)

    def test_inherited_role_words_in_child_goal_do_not_satisfy_role_coverage(self, tmp_path: Path):
        """后代 goal 继承了 tester/bug_finder/acceptor 字样，也不能冒充真实 QA 角色。"""
        from agent_py_agent.agent.subagents.manager_acceptance_findings import (
            SubAgentAcceptanceFindingMixin,
        )

        class MockManager(SubAgentAcceptanceFindingMixin):
            def __init__(self):
                self.workspace = tmp_path

        manager = MockManager()
        (tmp_path / "child").mkdir()
        (tmp_path / "child" / "task.json").write_text(
            json.dumps(
                {
                    "id": "child",
                    "role": "leaf_worker",
                    "agent_name": "小小小傻妞-001",
                    "goal": "继承父级层级约束：小小小傻妞-(tester/bug_finder/acceptor)",
                    "child_ids": [],
                }
            ),
            encoding="utf-8",
        )
        task = self._make_findings_task(
            tmp_path,
            goal="必须至少覆盖 tester / bug_finder / acceptor 三类 QA 子代理。",
            role="coordinator",
            child_ids=["child"],
        )
        output = {"artifacts": [], "structured_output": {"status": "COMPLETED"}}
        self._write_findings_files(tmp_path, output)
        manager.validate_work_order = MagicMock(return_value=MagicMock(ok=True, missing=[]))

        findings = manager._acceptance_findings(task, output, {}, time.time())

        self._assert_finding(findings, "required_role_coverage", expected_ok=False)

    def test_required_qa_roles_pass_when_descendants_cover_roles(self, tmp_path: Path):
        """真实后代 run 角色覆盖 tester/bug_finder/acceptor 时，QA 角色验收通过。"""
        from agent_py_agent.agent.subagents.manager_acceptance_findings import (
            SubAgentAcceptanceFindingMixin,
        )

        class MockManager(SubAgentAcceptanceFindingMixin):
            def __init__(self):
                self.workspace = tmp_path

        manager = MockManager()
        child_records = {
            "tester": {"id": "tester", "role": "tester", "child_ids": []},
            "bug": {"id": "bug", "role": "bug_finder", "child_ids": []},
            "accept": {"id": "accept", "role": "acceptor", "child_ids": []},
        }
        for run_id, record in child_records.items():
            (tmp_path / run_id).mkdir()
            (tmp_path / run_id / "task.json").write_text(json.dumps(record), encoding="utf-8")
        task = self._make_findings_task(
            tmp_path,
            goal="必须至少覆盖 tester / bug_finder / acceptor 三类 QA 子代理。",
            role="coordinator",
            child_ids=["tester", "bug", "accept"],
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
