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


# LLM: _write_child_task_json keeps descendant-health tests short while preserving real task.json layout.
# 函数用途: 写一个最小 child task.json，让验收扫描走真实文件路径而不是 mock。
def _write_child_task_json(root: Path, run_id: str, record: dict[str, object]) -> None:
    (root / run_id).mkdir()
    (root / run_id / "task.json").write_text(json.dumps(record), encoding="utf-8")


# LLM: _coverage_descendant_records keeps coverage tests focused on the health predicate.
# 函数用途: 返回一个损坏 leaf 和一个已验证覆盖 leaf 的最小 task.json 记录。
def _coverage_descendant_records() -> dict[str, dict[str, object]]:
    return {
        "bad-leaf": {
            "id": "bad-leaf",
            "role": "leaf_worker",
            "agent_name": "小小傻妞-越南监管",
            "status": "BLOCKED",
            "verification_status": "UNVERIFIED",
            "failure_type": "execution_corruption",
            "child_ids": [],
        },
        "good-leaf": {
            "id": "good-leaf",
            "role": "leaf_worker",
            "agent_name": "小小傻妞-越南综合",
            "status": "DONE",
            "verification_status": "VERIFIED",
            "child_ids": [],
        },
    }


# LLM: _coverage_task_attributes mirrors the runner-persisted coverage_records shape.
# 函数用途: 构造父级 coordinator 声明的 bad-leaf -> good-leaf 覆盖关系。
def _coverage_task_attributes() -> dict[str, object]:
    return {
        "coverage_records": [{
            "covered_run_id": "bad-leaf",
            "covered_by_run_id": "good-leaf",
            "reason": "good-leaf 覆盖 bad-leaf 的监管和痛点范围",
        }]
    }


class TestSubAgentAcceptanceOutputMiscFindings(_FindingSetupMixin, _FindingAssertMixin, _FindingReportMixin):
    """Misc output and artifact acceptance tests."""

    def test_required_child_attribute_without_child_ids_blocks_acceptance(self, tmp_path: Path):
        """任务机器字段要求创建下级时，不能只在结构化结果里伪造 child_run_ids。"""
        from agent_py_agent.agent.subagents.manager_acceptance_findings import (
            SubAgentAcceptanceFindingMixin,
        )

        class MockManager(SubAgentAcceptanceFindingMixin):
            def __init__(self):
                self.workspace = tmp_path

        manager = MockManager()
        task = self._make_findings_task(
            tmp_path,
            goal="普通说明里提到 child_spawn_required: true 也不能当机器合同。",
            attributes={"child_spawn_required": True, "required_child_depth": 3},
            child_ids=[],
        )
        output = {"artifacts": [], "structured_output": {"status": "COMPLETED"}}
        self._write_findings_files(tmp_path, output)
        manager.validate_work_order = MagicMock(return_value=MagicMock(ok=True, missing=[]))

        findings = manager._acceptance_findings(task, output, {}, time.time())

        self._assert_finding(findings, "required_child_spawned", expected_ok=False)

    def test_child_spawn_text_fields_do_not_require_child_ids(self, tmp_path: Path):
        """普通 goal/acceptance_checks 文本里的 child_spawn_required 不再触发机器验收门禁。"""
        from agent_py_agent.agent.subagents.manager_acceptance_findings import (
            SubAgentAcceptanceFindingMixin,
        )

        class MockManager(SubAgentAcceptanceFindingMixin):
            def __init__(self):
                self.workspace = tmp_path

        manager = MockManager()
        task = self._make_findings_task(
            tmp_path,
            goal="child_spawn_required: true\nrequired_child_depth: 3",
            acceptance_checks=["required_child_count: 2"],
            child_ids=[],
        )
        output = {"artifacts": [], "structured_output": {"status": "COMPLETED"}}
        self._write_findings_files(tmp_path, output)
        manager.validate_work_order = MagicMock(return_value=MagicMock(ok=True, missing=[]))

        findings = manager._acceptance_findings(task, output, {}, time.time())

        self._assert_finding(findings, "required_child_spawned", expected_ok=True)

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

    def test_collaboration_intent_is_runtime_log_not_acceptance_gate(self, tmp_path: Path):
        """协作意图不再由子代理验收硬卡；跑通流程后用协作账本/日志复盘。"""
        from agent_py_agent.agent.subagents.manager_acceptance_findings import (
            SubAgentAcceptanceFindingMixin,
        )

        class MockManager(SubAgentAcceptanceFindingMixin):
            def __init__(self):
                self.workspace = tmp_path

        manager = MockManager()
        task = self._make_findings_task(tmp_path)
        output = {
            "next_actions": [
                "父代理应协调其他负责方检查各自范围，并提交证据。"
            ],
            "structured_output": {
                "status": "COMPLETED",
                "summary": "发现一个高风险实体，需要跨来源验证。",
                "events": [{"requires_cross_verification": True}],
            },
        }
        self._write_findings_files(tmp_path, output)
        manager.validate_work_order = MagicMock(return_value=MagicMock(ok=True, missing=[]))

        findings = manager._acceptance_findings(task, output, {}, time.time())

        assert all(item.name != "collaboration_intent_resolved" for item in findings)



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
            goal="父级要做真实分工，完成后需要质量检查。",
            role="coordinator",
            child_ids=["child"],
            attributes={"required_qa_roles": ["tester", "bug_finder", "acceptor"]},
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
            goal="父级要做真实分工，完成后需要质量检查。",
            role="coordinator",
            child_ids=["child"],
            attributes={"required_qa_roles": ["tester", "bug_finder", "acceptor"]},
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
            goal="父级要做真实分工，完成后需要质量检查。",
            role="coordinator",
            child_ids=["tester", "bug", "accept"],
            attributes={"required_qa_roles": ["tester", "bug_finder", "acceptor"]},
        )
        output = {"artifacts": [], "structured_output": {"status": "COMPLETED"}}
        self._write_findings_files(tmp_path, output)
        manager.validate_work_order = MagicMock(return_value=MagicMock(ok=True, missing=[]))

        findings = manager._acceptance_findings(task, output, {}, time.time())

        self._assert_finding(findings, "required_role_coverage", expected_ok=True)

    def test_parent_with_unfinished_descendant_blocks_acceptance(self, tmp_path: Path):
        """父级不能在后代仍 PLANNING/UNVERIFIED 时被验收为完成。"""
        from agent_py_agent.agent.subagents.manager_acceptance_findings import (
            SubAgentAcceptanceFindingMixin,
        )

        class MockManager(SubAgentAcceptanceFindingMixin):
            def __init__(self):
                self.workspace = tmp_path

        manager = MockManager()
        (tmp_path / "tester").mkdir()
        (tmp_path / "tester" / "task.json").write_text(
            json.dumps(
                {
                    "id": "tester",
                    "role": "tester",
                    "agent_name": "小傻妞-tester",
                    "status": "PLANNING",
                    "verification_status": "UNVERIFIED",
                    "child_ids": [],
                }
            ),
            encoding="utf-8",
        )
        task = self._make_findings_task(
            tmp_path,
            goal="父级要做真实分工，完成后需要质量检查。",
            role="coordinator",
            child_ids=["tester"],
            attributes={"required_qa_roles": ["tester", "bug_finder", "acceptor"]},
        )
        output = {"artifacts": [], "structured_output": {"status": "COMPLETED"}}
        self._write_findings_files(tmp_path, output)
        manager.validate_work_order = MagicMock(return_value=MagicMock(ok=True, missing=[]))

        findings = manager._acceptance_findings(task, output, {}, time.time())

        self._assert_finding(findings, "descendant_health", expected_ok=False)

    def test_parent_with_verified_descendants_passes_descendant_health(self, tmp_path: Path):
        """所有后代 DONE/VERIFIED 时，父级后代健康门通过。"""
        from agent_py_agent.agent.subagents.manager_acceptance_findings import (
            SubAgentAcceptanceFindingMixin,
        )

        class MockManager(SubAgentAcceptanceFindingMixin):
            def __init__(self):
                self.workspace = tmp_path

        manager = MockManager()
        for run_id in ("tester", "bug", "accept"):
            (tmp_path / run_id).mkdir()
            (tmp_path / run_id / "task.json").write_text(
                json.dumps(
                    {
                        "id": run_id,
                        "role": {"bug": "bug_finder"}.get(run_id, run_id),
                        "status": "DONE",
                        "verification_status": "VERIFIED",
                        "child_ids": [],
                    }
                ),
                encoding="utf-8",
            )
        task = self._make_findings_task(
            tmp_path,
            goal="父级要做真实分工，完成后需要质量检查。",
            role="coordinator",
            child_ids=["tester", "bug", "accept"],
            attributes={"required_qa_roles": ["tester", "bug_finder", "acceptor"]},
        )
        output = {"artifacts": [], "structured_output": {"status": "COMPLETED"}}
        self._write_findings_files(tmp_path, output)
        manager.validate_work_order = MagicMock(return_value=MagicMock(ok=True, missing=[]))

        findings = manager._acceptance_findings(task, output, {}, time.time())

        self._assert_finding(findings, "descendant_health", expected_ok=True)
