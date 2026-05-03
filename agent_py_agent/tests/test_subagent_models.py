"""子代理数据模型测试 - models.py 数据类实例化、默认值、枚举值。"""
from __future__ import annotations

import time
from agent_py_agent.agent.subagents.models import (
    TaskStatus,
    DISPATCH_INELIGIBLE_STATUSES,
    QualityContract,
    ContextManifest,
    SubAgentCard,
    CapabilityRequest,
    CapabilityGrant,
    CapabilityGap,
    VerificationEvidence,
    WorkOrderValidation,
    TakeoverRecord,
    ChannelProbeCheck,
    ChannelProbeResult,
    ChannelProbeReport,
    SubAgentExecutionContext,
    SubAgentRunnerResult,
    SubAgentParsedOutput,
    SubAgentTask,
    LearningCandidate,
)


class TestTaskStatus:
    """TaskStatus 枚举测试。"""

    def test_task_status_values(self):
        """验证 TaskStatus 所有枚举值存在且唯一。"""
        assert TaskStatus.PLANNING.value == "PLANNING"
        assert TaskStatus.RUNNING.value == "RUNNING"
        assert TaskStatus.BLOCKED.value == "BLOCKED"
        assert TaskStatus.PAUSED.value == "PAUSED"
        assert TaskStatus.ABANDONED.value == "ABANDONED"
        assert TaskStatus.COMPLETED.value == "COMPLETED"
        assert TaskStatus.FAILED.value == "FAILED"

    def test_task_status_is_string_enum(self):
        """验证 TaskStatus 是字符串枚举。"""
        status = TaskStatus.RUNNING
        assert isinstance(status, str)
        assert status == "RUNNING"

    def test_dispatch_ineligible_statuses(self):
        """验证不可调度状态集合包含终止状态。"""
        assert "PAUSED" in DISPATCH_INELIGIBLE_STATUSES
        assert "ABANDONED" in DISPATCH_INELIGIBLE_STATUSES
        assert "COMPLETED" in DISPATCH_INELIGIBLE_STATUSES
        assert "FAILED" in DISPATCH_INELIGIBLE_STATUSES
        assert "RUNNING" not in DISPATCH_INELIGIBLE_STATUSES
        assert "PLANNING" not in DISPATCH_INELIGIBLE_STATUSES
        assert "BLOCKED" not in DISPATCH_INELIGIBLE_STATUSES


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
        assert qc.final_judge == "parent_final_gate"
        assert qc.cannot_self_accept is True
        assert qc.parent_final_gate is True

    def test_quality_contract_with_values(self):
        """验证自定义值。"""
        qc = QualityContract(
            user_visible_goal="完成登录功能",
            quality_bar="所有测试通过",
            failure_conditions=["编译错误", "运行崩溃"],
            cannot_self_accept=False,
        )
        assert qc.user_visible_goal == "完成登录功能"
        assert qc.quality_bar == "所有测试通过"
        assert qc.failure_conditions == ["编译错误", "运行崩溃"]
        assert qc.cannot_self_accept is False


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
