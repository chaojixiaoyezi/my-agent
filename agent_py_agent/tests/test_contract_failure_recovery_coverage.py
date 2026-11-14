# LLM: 开发专用离线合同只在唯一离线测试中导入；其余六节点必须只依赖正式发行模块并验证原恢复断言。
# 模块用途: 按原发行边界分开开发校验器与运行时恢复回执，避免离线依赖阻止整份真实包测试收集。
from __future__ import annotations

from agent_py_agent.agent.contracts.artifact_acceptance_models import (
    ArtifactAcceptanceReport,
    ArtifactFinding,
)
from agent_py_agent.agent.contracts.effective_contract_snapshot import (
    build_effective_contract_snapshot,
    validate_replay_effective_contract,
)
from agent_py_agent.agent.contracts.evidence_contract import EvidenceContractReport
from agent_py_agent.agent.contracts.gates.models import GateDecision, GateFinding


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


# LLM: 这里只调用正式包内校验器；原输入与逐项ok=False/恢复断言保留。
# 函数用途: 用正式合同构造失败，确保包内运行仍返回结构化恢复事实。
def test_runtime_contract_validation_failures_return_recovery() -> None:
    snapshot = build_effective_contract_snapshot(run_id="run-1", layers=({"artifacts": {"required": []}},))
    for result in [
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
