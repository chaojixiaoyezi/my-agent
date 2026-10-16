# LLM: 开发专用离线合同只在唯一离线测试中导入；其余六节点必须只依赖正式发行模块并验证原恢复断言。
# 模块用途: 按原发行边界分开开发校验器与运行时恢复回执，避免离线依赖阻止整份真实包测试收集。
from __future__ import annotations

from agent_py_agent.agent.contracts.approval_gate import ApprovalGateDecision
from agent_py_agent.agent.contracts.artifact_acceptance_models import (
    ArtifactAcceptanceReport,
    ArtifactFinding,
)
from agent_py_agent.agent.contracts.contract_doctor import lint_contract
from agent_py_agent.agent.contracts.effective_contract_snapshot import (
    build_effective_contract_snapshot,
    validate_replay_effective_contract,
)
from agent_py_agent.agent.contracts.evidence_contract import EvidenceContractReport
from agent_py_agent.agent.contracts.gates.models import GateDecision, GateFinding
from agent_py_agent.agent.contracts.runtime_cards import RuntimeCard, validate_runtime_card_set


# LLM: 所有合同失败共用相同结构化恢复断言，分组移动不能删减状态、动作或中文提示的检查。
# 函数用途: 检查对象或原to_dict投影中的恢复事实；只读取结果，不发请求或修改状态。
def _assert_recovery(payload: object) -> None:
    recovery = getattr(payload, "recovery", None)
    if recovery is None and hasattr(payload, "to_dict"):
        recovery = payload.to_dict().get("recovery")
    assert isinstance(recovery, dict)
    assert recovery["status"] in {"repair_required", "needs_user_input", "recovering", "blocked"}
    assert recovery["actions"]
    assert recovery["actions"][0]["message_zh"]


# LLM: 这些校验器由唯一package_boundary_policy判为开发专用；保留原输入及失败恢复断言，不能补入发行包。
# 函数用途: 在源码环境验证十二项开发合同，延迟导入使其他运行时节点仍可在真实pyc包内收集执行。
def test_offline_contract_validation_failures_return_recovery() -> None:
    from agent_py_agent.agent.contracts.offline_compact_resume_contract import (
        validate_compact_resume_bundle,
    )
    from agent_py_agent.agent.contracts.offline_concurrency_contract import (
        validate_concurrency_events,
    )
    from agent_py_agent.agent.contracts.offline_contract_report import validation_report
    from agent_py_agent.agent.contracts.offline_memory_skill_contract import (
        validate_memory_skill_contract,
    )
    from agent_py_agent.agent.contracts.offline_output_format_contract import (
        validate_output_format_contract,
    )
    from agent_py_agent.agent.contracts.offline_plan_contract import validate_plan_contract
    from agent_py_agent.agent.contracts.offline_security_boundary_contract import (
        validate_security_boundary_events,
    )
    from agent_py_agent.agent.contracts.offline_subagent_contract import validate_subagent_contract
    from agent_py_agent.agent.contracts.offline_tool_contract import validate_tool_events
    from agent_py_agent.agent.contracts.offline_tool_guardrail_contract import (
        validate_tool_guardrail_events,
    )
    from agent_py_agent.agent.contracts.run_trace_contract import validate_run_trace_events
    from agent_py_agent.agent.contracts.runtime_config_contract import validate_runtime_config

    for result in [
        validation_report([{"code": "GENERIC_CONTRACT_FINDING"}]),
        validate_output_format_contract({"outputs": [{"kind": "json", "required_fields": ["a"], "fields": []}]}),
        validate_plan_contract(plan={}, contract={"artifacts": {"required": [{"path": "out.md"}]}}),
        validate_run_trace_events(({"type": "state_transition", "from": "RUNNING", "to": "DONE"},)),
        validate_compact_resume_bundle({"pre_compact": {"current_status": "WAITING_FOR_TOOL"}, "compact": {}}),
        validate_tool_events(({"type": "tool_result", "result": None},)),
        validate_tool_guardrail_events(({"type": "tool_result", "tool": "read", "result": {}},)),
        validate_memory_skill_contract({"memory_writes": [{"memory_ref": "m1"}]}),
        validate_concurrency_events((
            {"type": "lease_claimed", "run_id": "run-1", "worker_id": "a"},
            {"type": "lease_claimed", "run_id": "run-1", "worker_id": "b"},
        )),
        validate_subagent_contract({"parent": {"run_id": "p", "status": "DONE", "required_child_run_ids": ["c"]}}),
        validate_security_boundary_events(({"type": "network_request", "url": "file:///etc/passwd"},)),
        validate_runtime_config({}),
    ]:
        assert result.ok is False
        _assert_recovery(result)


# LLM: 这里只调用正式包内校验器；原十五项输入总量与逐项ok=False/恢复断言须与离线节点合计保持。
# 函数用途: 用正式合同构造三种失败，确保包内运行仍返回结构化恢复事实而非依赖离线测试模块。
def test_runtime_contract_validation_failures_return_recovery() -> None:
    snapshot = build_effective_contract_snapshot(run_id="run-1", layers=({"artifacts": {"required": []}},))
    for result in [
        lint_contract({"max_steps": "many"}),
        validate_runtime_card_set([RuntimeCard(kind="task", card_id="task-1", owner_user_id="u", status="RUNNING")]),
        validate_replay_effective_contract({"contract_hash": "sha256:old"}, snapshot),
    ]:
        assert result.ok is False
        _assert_recovery(result)


def test_artifact_and_evidence_reports_include_recovery_when_rejected() -> None:
    reports = [
        ArtifactAcceptanceReport(
            ok=False,
            artifact_ref="outputs/missing.md",
            findings=[ArtifactFinding("ARTIFACT_MISSING", "hard", "missing")],
        ),
        EvidenceContractReport(ok=False, summary={}, findings=[{"code": "EVIDENCE_SOURCE_UNREADABLE"}]),
    ]

    for report in reports:
        _assert_recovery(report)


def test_approval_waiting_failures_request_user_input_instead_of_blocking() -> None:
    for decision in [
        ApprovalGateDecision(False, "WAITING_HUMAN", "APPROVAL_NOT_FOUND", "approval-1"),
        ApprovalGateDecision(False, "WAITING_HUMAN", "APPROVAL_PENDING", "approval-1"),
    ]:
        recovery = decision.to_dict()["recovery"]
        assert recovery["status"] == "needs_user_input"
        assert recovery["requires_user"] is True
        assert recovery["next_status"] == "WAITING_APPROVAL"


def test_approval_terminal_failures_block_instead_of_waiting_for_language() -> None:
    for code in [
        "APPROVAL_ALREADY_USED",
        "APPROVAL_BINDING_MISMATCH",
        "APPROVAL_EXPIRED",
        "APPROVAL_REJECTED",
        "APPROVER_NOT_AUTHORIZED",
    ]:
        recovery = ApprovalGateDecision(False, "BLOCKED", code, "approval-1").to_dict()["recovery"]
        assert recovery["status"] == "blocked"
        assert recovery["requires_user"] is False
        assert recovery["terminal"] is True
        assert recovery["next_status"] == "BLOCKED"


def test_recovery_classification_normalizes_structured_codes_only() -> None:
    from agent_py_agent.agent.contracts.recovery import (
        action_status,
        recommended_action,
        recovery_category,
    )

    assert action_status("WAITING_HUMAN", "approval-required") == "needs_user_input"
    assert action_status("BLOCKED", "approval-rejected") == "blocked"
    assert action_status("NEED_REPAIR", "artifact-missing") == "repair_required"
    assert recovery_category("path-outside-workspace") == "path"
    assert recommended_action("tool-invalid-arguments", "repair_required") == "repair_tool_arguments"
    assert action_status("", "approval rejected") == "blocked"
    assert action_status("need_repair", "artifact-missing") == "blocked"
    assert recommended_action("unknown-finding", "NEEDS_USER_INPUT") == "repair_against_contract_findings"


def test_gate_decision_failures_use_consistent_recovery_statuses() -> None:
    rows = [
        (GateDecision.repair("artifact_gate", [GateFinding("ARTIFACT_MISSING")]), "repair_required"),
        (GateDecision.need_approval("approval_gate"), "needs_user_input"),
        (GateDecision.recovering("runlog_gate", [GateFinding("RUNLOG_STATE_MISSING")]), "recovering"),
        (GateDecision.block("state_gate", "STATE_CHECKSUM_MISMATCH"), "blocked"),
    ]
    for decision, expected_status in rows:
        recovery = decision.to_dict()["recovery"]
        assert recovery["status"] == expected_status
        assert recovery["actions"]
