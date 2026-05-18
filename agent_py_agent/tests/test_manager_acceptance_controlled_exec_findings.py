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


class TestControlledExecAcceptanceFindings(_FindingSetupMixin, _FindingAssertMixin, _FindingReportMixin):
    """Controlled exec contract acceptance tests."""

    def test_controlled_exec_attribute_requires_actual_tool_and_refs(self, tmp_path: Path):
        """结构化声明 controlled_exec 的任务不能只用 write_file 伪造完成。"""
        from agent_py_agent.agent.subagents.manager_acceptance_findings import (
            SubAgentAcceptanceFindingMixin,
        )

        class MockManager(SubAgentAcceptanceFindingMixin):
            def __init__(self):
                self.workspace = tmp_path

        manager = MockManager()
        task = self._make_findings_task(
            tmp_path,
            goal="执行 pwd，并报告 stdout_ref、audit_ref、trash_manifest_ref。",
            used_tools=["write_file"],
            attributes={"controlled_exec_contract_required": True},
        )
        output = {"used_tools": ["write_file"], "evidence_packets": []}
        self._write_findings_files(tmp_path, output)
        manager.validate_work_order = MagicMock(return_value=MagicMock(ok=True, missing=[]))

        findings = manager._acceptance_findings(task, output, {}, time.time())

        self._assert_finding(findings, "controlled_exec_contract_satisfied", expected_ok=False)

    def test_controlled_exec_text_mentions_do_not_require_contract(self, tmp_path: Path):
        """普通说明文字提到 controlled_exec 时，不会被当成机器验收合同。"""
        from agent_py_agent.agent.subagents.manager_acceptance_findings import (
            SubAgentAcceptanceFindingMixin,
        )

        class MockManager(SubAgentAcceptanceFindingMixin):
            def __init__(self):
                self.workspace = tmp_path

        manager = MockManager()
        task = self._make_findings_task(
            tmp_path,
            goal="说明里可以提 controlled_exec 这个词，但没有结构化要求。",
            acceptance_checks=["不要把 controlled_exec 这个普通词当成硬合同。"],
            used_tools=["write_file"],
        )
        output = {"used_tools": ["write_file"], "evidence_packets": []}
        self._write_findings_files(tmp_path, output)
        manager.validate_work_order = MagicMock(return_value=MagicMock(ok=True, missing=[]))

        findings = manager._acceptance_findings(task, output, {}, time.time())

        self._assert_finding(findings, "controlled_exec_contract_satisfied", expected_ok=True)

    def test_controlled_exec_contract_reads_deliverable_refs(self, tmp_path: Path):
        """controlled_exec 真实 refs 写到交付目录时，也能通过验收。"""
        from agent_py_agent.agent.subagents.manager_acceptance_findings import (
            SubAgentAcceptanceFindingMixin,
        )

        class MockManager(SubAgentAcceptanceFindingMixin):
            def __init__(self):
                self.workspace = tmp_path

        deliverables = tmp_path / "deliverables"
        deliverables.mkdir()
        (deliverables / "controlled_exec_refs.json").write_text(
            json.dumps({"stdout_ref": "stdout.txt", "trash_manifest_ref": "trash/manifest.jsonl"}),
            encoding="utf-8",
        )
        (deliverables / "controlled_exec_test_results.md").write_text("audit_ref: audit.jsonl", encoding="utf-8")
        manager = MockManager()
        task = self._make_findings_task(
            tmp_path,
            goal="执行 pwd/rm，并报告 stdout_ref、audit_ref、trash_manifest_ref。",
            used_tools=["controlled_exec"],
            allowed_write_roots=[str(deliverables)],
            attributes={"controlled_exec_contract_required": True},
        )
        output = {"used_tools": ["controlled_exec"], "evidence_packets": []}
        self._write_findings_files(tmp_path, output)
        manager.validate_work_order = MagicMock(return_value=MagicMock(ok=True, missing=[]))

        findings = manager._acceptance_findings(task, output, {}, time.time())

        self._assert_finding(findings, "controlled_exec_contract_satisfied", expected_ok=True)

    def test_controlled_exec_contract_accepts_descendant_tool_evidence(self, tmp_path: Path):
        """coordinator 只派工时，可用下级真实 controlled_exec 记录配合 refs 通过验收。"""
        from agent_py_agent.agent.subagents.manager_acceptance_findings import (
            SubAgentAcceptanceFindingMixin,
        )

        class MockManager(SubAgentAcceptanceFindingMixin):
            def __init__(self):
                self.workspace = tmp_path / "subagents"

        parent_dir, deliverables = _write_descendant_controlled_exec_tree(tmp_path)
        manager = MockManager()
        task = self._make_findings_task(
            tmp_path,
            task_dir=parent_dir,
            goal="创建下级执行 shell 任务，并报告 stdout_ref、audit_ref、trash_manifest_ref。",
            role="coordinator",
            child_ids=["child"],
            allowed_write_roots=[str(deliverables)],
            attributes={"required_tool_evidence": ["controlled_exec"]},
        )
        output = {"used_tools": ["dispatch_subagents"], "evidence_packets": []}
        self._write_findings_files(tmp_path, output)
        manager.validate_work_order = MagicMock(return_value=MagicMock(ok=True, missing=[]))

        findings = manager._acceptance_findings(task, output, {}, time.time())

        self._assert_finding(findings, "controlled_exec_contract_satisfied", expected_ok=True)

    def test_controlled_exec_contract_rejects_refs_without_tool_evidence(self, tmp_path: Path):
        """有 refs 但没有当前或下级真实 controlled_exec 工具记录时，仍然拒绝验收。"""
        from agent_py_agent.agent.subagents.manager_acceptance_findings import (
            SubAgentAcceptanceFindingMixin,
        )

        class MockManager(SubAgentAcceptanceFindingMixin):
            def __init__(self):
                self.workspace = tmp_path / "subagents"

        workspace = tmp_path / "subagents"
        parent_dir = workspace / "parent"
        child_dir = workspace / "child"
        deliverables = tmp_path / "deliverables"
        for path in (parent_dir, child_dir, deliverables):
            path.mkdir(parents=True)
        (child_dir / "task.json").write_text(
            json.dumps({"id": "child", "used_tools": ["write_file"], "child_ids": []}),
            encoding="utf-8",
        )
        (deliverables / "controlled_exec_refs.json").write_text(
            json.dumps({
                "stdout_ref": "stdout.json",
                "audit_ref": "audit.jsonl",
                "trash_manifest_ref": "trash/manifest.jsonl",
            }),
            encoding="utf-8",
        )
        manager = MockManager()
        task = self._make_findings_task(
            tmp_path,
            task_dir=parent_dir,
            goal="创建下级执行 shell 任务，并报告 stdout_ref、audit_ref、trash_manifest_ref。",
            role="coordinator",
            child_ids=["child"],
            allowed_write_roots=[str(deliverables)],
            attributes={"controlled_exec_contract_required": True},
        )
        output = {"used_tools": ["dispatch_subagents"], "evidence_packets": []}
        self._write_findings_files(tmp_path, output)
        manager.validate_work_order = MagicMock(return_value=MagicMock(ok=True, missing=[]))

        findings = manager._acceptance_findings(task, output, {}, time.time())

        self._assert_finding(findings, "controlled_exec_contract_satisfied", expected_ok=False)


# LLM: _ControlledExecHarness reuses acceptance mixin helpers without growing the main test class.
# 类用途: 给类外 focused tests 复用 _make_findings_task / _assert_finding / _write_findings_files。
class _ControlledExecHarness(_FindingSetupMixin, _FindingAssertMixin, _FindingReportMixin):
    pass


# LLM: test artifact refs following in a small standalone shape to keep code-size clean.
# 函数用途: refs 文件只写 artifact_ref 时，验收可读取受控小 artifact 里的 stdout/audit refs。
def test_controlled_exec_contract_follows_small_tool_output_artifact_refs(tmp_path: Path):
    harness = _ControlledExecHarness()
    manager = _controlled_exec_manager(tmp_path)
    deliverables = _write_controlled_exec_artifact_ref_case(tmp_path)
    task = harness._make_findings_task(
        tmp_path,
        goal="执行 pwd/rm，并报告 stdout_ref、audit_ref、trash_manifest_ref。",
        used_tools=["controlled_exec"],
        allowed_write_roots=[str(deliverables)],
        attributes={"controlled_exec_contract_required": True},
    )
    output = {"used_tools": ["controlled_exec"], "evidence_packets": []}
    harness._write_findings_files(tmp_path, output)
    manager.validate_work_order = MagicMock(return_value=MagicMock(ok=True, missing=[]))

    findings = manager._acceptance_findings(task, output, {}, time.time())

    harness._assert_finding(findings, "controlled_exec_contract_satisfied", expected_ok=True)


# LLM: _controlled_exec_manager creates the acceptance mixin facade used by focused tests.
# 函数用途: 生成带 workspace 和 validate_work_order 的最小 manager，避免每个测试重复内部类。
def _controlled_exec_manager(workspace: Path):
    from agent_py_agent.agent.subagents.manager_acceptance_findings import (
        SubAgentAcceptanceFindingMixin,
    )

    class MockManager(SubAgentAcceptanceFindingMixin):
        def __init__(self):
            self.workspace = workspace

    return MockManager()


# LLM: _write_controlled_exec_artifact_ref_case keeps artifact-ref fixture setup outside test logic.
# 函数用途: 写入 controlled_exec_refs.json 和对应 tool_output artifact，返回可验收的 deliverables 目录。
def _write_controlled_exec_artifact_ref_case(tmp_path: Path) -> Path:
    deliverables = tmp_path / "deliverables"
    artifact_dir = tmp_path / "memory_archive" / "artifacts" / "tool_outputs"
    deliverables.mkdir()
    artifact_dir.mkdir(parents=True)
    artifact = artifact_dir / "controlled_exec-1-1-deadbeef.json"
    artifact.write_text(
        json.dumps({
            "tool": "controlled_exec",
            "content": json.dumps({"execution": _artifact_execution_refs(tmp_path)}),
        }),
        encoding="utf-8",
    )
    (deliverables / "controlled_exec_refs.json").write_text(
        json.dumps({
            "command_results": {"pwd": {"artifact_ref": str(artifact)}},
            "trash_manifest_ref": str(tmp_path / "trash" / "manifest.jsonl"),
        }),
        encoding="utf-8",
    )
    return deliverables


# LLM: _artifact_execution_refs returns the machine refs expected inside a controlled_exec artifact.
# 函数用途: 统一 stdout_ref/audit_ref 测试数据，保持 fixture setup 短小。
def _artifact_execution_refs(tmp_path: Path) -> dict[str, str]:
    return {
        "stdout_ref": str(tmp_path / "stdout.txt"),
        "audit_ref": str(tmp_path / "audit.jsonl"),
    }


# LLM: _write_descendant_controlled_exec_tree keeps the coordinator acceptance test below size limits.
# 函数用途: 构造 parent -> child -> leaf 的真实 controlled_exec 证据树，供验收测试复用。
def _write_descendant_controlled_exec_tree(tmp_path: Path) -> tuple[Path, Path]:
    workspace = tmp_path / "subagents"
    parent_dir = workspace / "parent"
    child_dir = workspace / "child"
    leaf_dir = workspace / "leaf"
    deliverables = tmp_path / "deliverables"
    for path in (parent_dir, child_dir, leaf_dir, deliverables):
        path.mkdir(parents=True)
    (child_dir / "task.json").write_text(
        json.dumps({"id": "child", "used_tools": [], "child_ids": ["leaf"]}),
        encoding="utf-8",
    )
    (leaf_dir / "task.json").write_text(
        json.dumps({"id": "leaf", "used_tools": ["controlled_exec"], "child_ids": []}),
        encoding="utf-8",
    )
    (deliverables / "controlled_exec_refs.json").write_text(
        json.dumps({
            "stdout_refs": {"pwd": "stdout.json"},
            "audit_refs": {"pwd": "audit.jsonl"},
            "trash_manifest_ref": "trash/manifest.jsonl",
        }),
        encoding="utf-8",
    )
    return parent_dir, deliverables
