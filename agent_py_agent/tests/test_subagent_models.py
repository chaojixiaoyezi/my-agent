"""子代理数据模型测试 - models.py 数据类实例化、默认值、枚举值。"""
from __future__ import annotations

import time
from types import SimpleNamespace

from agent_py_agent.agent.core import SimpleAgent
from agent_py_agent.agent.settings.config import AgentConfig
from agent_py_agent.agent.subagents.result_structured_evidence import process_evidence_items


def test_simple_agent_passes_closeout_task_node_config(tmp_path):
    config = AgentConfig(
        model_backend="echo",
        closeout_for_all_task_nodes=True,
        local_store_path=str(tmp_path / "local.db"),
        local_store_files_dir=str(tmp_path / "files"),
        local_store_events_path=str(tmp_path / "events.jsonl"),
        memory_path=str(tmp_path / "memory.jsonl"),
        subagent_workspace=str(tmp_path / "subagents"),
        conversation_workspace=str(tmp_path / "conversation"),
        collaboration_workspace=str(tmp_path / "collaboration"),
    )

    agent = SimpleAgent(config, tmp_path)

    assert agent.subagents.closeout_for_all_task_nodes is True


def test_subagent_evidence_without_explicit_ok_is_not_success() -> None:
    task = SubAgentTask(id="child-1", goal="check", thought="", plan=[])
    parsed = SimpleNamespace(
        evidence=[
            {"kind": "note", "summary": "missing ok"},
            {
                "kind": "content_check",
                "summary": "absence is expected",
                "ok": False,
                "content_pattern": "needle",
                "match_mode": "not_contains",
            },
        ]
    )

    count = process_evidence_items(parsed, task, now=1.0)

    assert count == 2
    assert task.evidence[0].ok is False
    assert task.evidence[1].ok is True


from agent_py_agent.agent.subagents.models import (
    DISPATCH_INELIGIBLE_STATUSES,
    SUBAGENT_TASK_STATUSES,
    CapabilityGap,
    CapabilityGrant,
    CapabilityRequest,
    ChannelProbeCheck,
    ChannelProbeReport,
    ChannelProbeResult,
    ContextManifest,
    LearningCandidate,
    QualityContract,
    SubAgentCard,
    SubAgentExecutionContext,
    SubAgentParsedOutput,
    SubAgentRunnerResult,
    SubAgentTask,
    TakeoverRecord,
    TaskStatus,
    VerificationEvidence,
    WorkOrderValidation,
    normalize_task_status,
)


class TestTaskStatus:
    """TaskStatus 枚举测试。"""

    def test_task_status_values(self):
        """验证 TaskStatus 所有枚举值存在且唯一。"""
        assert TaskStatus.PLANNING.value == "PLANNING"
        assert TaskStatus.PENDING.value == "PENDING"
        assert TaskStatus.RUNNING.value == "RUNNING"
        assert TaskStatus.BLOCKED.value == "BLOCKED"
        assert TaskStatus.PAUSED.value == "PAUSED"
        assert TaskStatus.ABANDONED.value == "ABANDONED"
        assert TaskStatus.CANCELLED.value == "CANCELLED"
        assert TaskStatus.DONE.value == "DONE"
        assert TaskStatus.FAILED.value == "FAILED"
        assert TaskStatus.TIMEOUT.value == "TIMEOUT"
        assert TaskStatus.CHANNEL_ERROR.value == "CHANNEL_ERROR"

    def test_task_status_is_string_enum(self):
        """验证 TaskStatus 是字符串枚举。"""
        status = TaskStatus.RUNNING
        assert isinstance(status, str)
        assert status == "RUNNING"

    def test_dispatch_ineligible_statuses(self):
        """验证不可调度状态集合包含终止状态。"""
        assert "PAUSED" in DISPATCH_INELIGIBLE_STATUSES
        assert "ABANDONED" in DISPATCH_INELIGIBLE_STATUSES
        assert "CANCELLED" in DISPATCH_INELIGIBLE_STATUSES
        assert "DONE" in DISPATCH_INELIGIBLE_STATUSES
        assert "FAILED" in DISPATCH_INELIGIBLE_STATUSES
        assert "TIMEOUT" in DISPATCH_INELIGIBLE_STATUSES
        assert "CHANNEL_ERROR" in DISPATCH_INELIGIBLE_STATUSES
        assert "RUNNING" not in DISPATCH_INELIGIBLE_STATUSES
        assert "PLANNING" not in DISPATCH_INELIGIBLE_STATUSES
        assert "PENDING" not in DISPATCH_INELIGIBLE_STATUSES
        assert "BLOCKED" not in DISPATCH_INELIGIBLE_STATUSES

    def test_dispatch_ineligible_all_values_are_strings(self):
        """验证 DISPATCH_INELIGIBLE_STATUSES 所有元素都是字符串值。

        Mutation: Missing .value on DONE (e.g., TaskStatus.DONE instead of .value)
        This would break membership checks since TaskStatus != "DONE"
        """
        for status in DISPATCH_INELIGIBLE_STATUSES:
            assert isinstance(status, str), f"{status} is not a string, got {type(status).__name__}"
            assert len(status) > 0, "Empty string in DISPATCH_INELIGIBLE_STATUSES"
        # All statuses should match their string enum values
        assert "PAUSED" in DISPATCH_INELIGIBLE_STATUSES
        assert "ABANDONED" in DISPATCH_INELIGIBLE_STATUSES
        assert "DONE" in DISPATCH_INELIGIBLE_STATUSES
        assert "FAILED" in DISPATCH_INELIGIBLE_STATUSES

    def test_normalize_task_status_accepts_current_protocol_only(self):
        assert normalize_task_status("done") == "DONE"
        assert normalize_task_status("channel_error") == "CHANNEL_ERROR"
        assert "COMPLETED" not in SUBAGENT_TASK_STATUSES
        try:
            normalize_task_status("completed")
        except ValueError as exc:
            assert str(exc) == "subagent_status_invalid"
        else:
            raise AssertionError("non-protocol status must fail closed")

    def test_dispatch_ineligible_status_is_not_enum_member(self):
        """验证 DISPATCH_INELIGIBLE_STATUSES 包含字符串值而非枚举成员。"""
        # The frozenset should contain actual string values like "DONE"
        # Not enum members like TaskStatus.DONE
        assert "DONE" in DISPATCH_INELIGIBLE_STATUSES
        assert "FAILED" in DISPATCH_INELIGIBLE_STATUSES
        # If mutation removed .value, some elements would be TaskStatus (not str)
        for status in DISPATCH_INELIGIBLE_STATUSES:
            assert type(status) is str, f"Element {status!r} is {type(status).__name__}, not str"


class TestQualityContract:
    """QualityContract 数据类测试。"""

    def test_quality_contract_defaults(self):
        """验证默认值。"""
        qc = QualityContract()
        assert qc.user_visible_goal == ""
        assert qc.benchmark_sample == ""
        assert qc.quality_bar == ""
        assert qc.failure_conditions == []
        assert qc.forbidden_delivery == []
        assert qc.must_check == []
        assert qc.sampling_plan == []
        assert qc.evidence_required == []
        assert qc.risk_report_required == ""
        assert qc.allowed_degradation == []

    def test_quality_contract_with_values(self):
        """验证自定义值。"""
        qc = QualityContract(
            user_visible_goal="完成登录功能",
            quality_bar="所有测试通过",
            failure_conditions=["编译错误", "运行崩溃"],
        )
        assert qc.user_visible_goal == "完成登录功能"
        assert qc.quality_bar == "所有测试通过"
        assert qc.failure_conditions == ["编译错误", "运行崩溃"]


class TestContextManifest:
    """ContextManifest 数据类测试。"""

    def test_context_manifest_defaults(self):
        """验证默认值。"""
        cm = ContextManifest()
        assert cm.core_pack_version == "subagent-quality-contract-v1"
        assert cm.task_pack_refs == []
        assert cm.role_pack == ""
        assert cm.required_read_paths == []
        assert cm.quality_contract_ref == ""
        assert cm.omitted_context == []
        assert cm.token_budget == 0

    def test_context_manifest_with_paths(self):
        """验证带路径列表的实例化。"""
        cm = ContextManifest(
            task_pack_refs=["ref1", "ref2"],
            required_read_paths=["/path/to/file1", "/path/to/file2"],
            token_budget=5000,
        )
        assert len(cm.task_pack_refs) == 2
        assert len(cm.required_read_paths) == 2
        assert cm.token_budget == 5000


class TestSubAgentCard:
    """SubAgentCard 数据类测试。"""

    def test_subagent_card_required_fields(self):
        """验证必需字段。"""
        card = SubAgentCard(name="test", description="test desc")
        assert card.name == "test"
        assert card.description == "test desc"

    def test_subagent_card_defaults(self):
        """验证默认值。"""
        card = SubAgentCard(name="test", description="desc")
        assert card.role == "general"
        assert card.default_model == "inherit"
        assert card.allowed_skills == []
        assert card.allowed_tools == []
        assert card.can_write is False
        assert card.can_spawn_children is False
        assert card.can_request_capability is True
        assert card.max_depth == 0
        assert card.result_contract == []


class TestCapabilityRequest:
    """CapabilityRequest 数据类测试。"""

    def test_capability_request_required_fields(self):
        """验证必需字段。"""
        req = CapabilityRequest(
            id="req-1",
            from_run_id="run-1",
            problem="需要某能力",
            needed_capability="some_capability",
        )
        assert req.id == "req-1"
        assert req.from_run_id == "run-1"
        assert req.problem == "需要某能力"
        assert req.needed_capability == "some_capability"

    def test_capability_request_defaults(self):
        """验证默认值。"""
        req = CapabilityRequest(id="req-1", from_run_id="run-1", problem="p", needed_capability="c")
        assert req.expected_output == ""
        assert req.tried == []
        assert req.evidence == []
        assert req.constraints == {}
        assert req.status == "OPEN"
        assert req.created_at == 0.0


class TestSubAgentParsedOutput:
    """SubAgentParsedOutput 数据类测试。"""

    def test_parsed_output_defaults(self):
        """验证默认值。"""
        output = SubAgentParsedOutput()
        assert output.found is False
        assert output.ok is False
        assert output.parse_error == ""
        assert output.status == ""
        assert output.summary == ""
        assert output.blocked_reason == ""
        assert output.failure_type == ""
        assert output.used_skills == []
        assert output.used_tools == []
        assert output.evidence == []
        assert output.capability_requests == []
        assert output.artifacts == []
        assert output.tests == []
        assert output.patches == []
        assert output.lessons == []
        assert output.next_actions == []
        assert output.raw_json == {}

    def test_parsed_output_with_values(self):
        """验证带值实例化。"""
        output = SubAgentParsedOutput(
            found=True,
            ok=True,
            status="COMPLETED",
            summary="任务完成",
            used_skills=["skill1"],
            used_tools=["tool1"],
        )
        assert output.found is True
        assert output.ok is True
        assert output.status == "COMPLETED"
        assert "skill1" in output.used_skills
        assert "tool1" in output.used_tools


class TestSubAgentTask:
    """SubAgentTask 数据类测试。"""

    def test_subagent_task_required_fields(self):
        """验证必需字段。"""
        task = SubAgentTask(
            id="task-1",
            goal="完成某事",
            thought="思考过程",
            plan=["步骤1", "步骤2"],
        )
        assert task.id == "task-1"
        assert task.goal == "完成某事"
        assert task.thought == "思考过程"
        assert task.plan == ["步骤1", "步骤2"]

    def test_subagent_task_defaults(self):
        """验证默认值。"""
        task = SubAgentTask(
            id="task-1",
            goal="goal",
            thought="thought",
            plan=[],
        )
        assert task.agent_name == "general"
        assert task.role == "general"
        assert task.owner == ""
        assert task.status == TaskStatus.PLANNING.value
        assert task.verification_status == "UNVERIFIED"
        assert task.runner_attempts == 0
        assert task.allowed_skills == []
        assert task.allowed_tools == []
        assert task.used_skills == []
        assert task.used_tools == []
        assert task.capability_requests == []
        assert task.capability_grants == []
        assert task.capability_gaps == []
        assert task.evidence == []
        assert task.child_ids == []

    def test_subagent_task_nested_dataclasses(self):
        """验证嵌套数据类。"""
        task = SubAgentTask(
            id="task-1",
            goal="goal",
            thought="thought",
            plan=[],
            quality_contract=QualityContract(user_visible_goal="自定义目标"),
        )
        assert task.quality_contract.user_visible_goal == "自定义目标"
        assert isinstance(task.context_manifest, ContextManifest)
