from __future__ import annotations

from agent_py_agent.agent.contracts.acceptance_contract import AcceptanceResult
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
from agent_py_agent.agent.contracts.main_agent_real_task_acceptance import (
    RealTaskAcceptanceReport,
)
from agent_py_agent.agent.contracts.main_agent_task_acceptance import (
    TaskRunAcceptanceReport,
)
from agent_py_agent.agent.contracts.offline_compact_resume_contract import (
    validate_compact_resume_bundle,
)
from agent_py_agent.agent.contracts.offline_concurrency_contract import validate_concurrency_events
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
from agent_py_agent.agent.contracts.offline_verifier_integrity_contract import (
    validate_verifier_integrity,
)
from agent_py_agent.agent.contracts.run_trace_contract import validate_run_trace_events
from agent_py_agent.agent.contracts.runtime_cards import RuntimeCard, validate_runtime_card_set
from agent_py_agent.agent.contracts.runtime_config_contract import validate_runtime_config


def _assert_recovery(payload: object) -> None:
    recovery = getattr(payload, "recovery", None)
    if recovery is None and hasattr(payload, "to_dict"):
        recovery = payload.to_dict().get("recovery")
    assert isinstance(recovery, dict)
    assert recovery["status"] in {"repair_required", "needs_user_input", "recovering", "blocked"}
    assert recovery["actions"]
    assert recovery["actions"][0]["message_zh"]


def test_offline_contract_validation_failures_return_recovery() -> None:
    for result in [
        validation_report([{"code": "GENERIC_CONTRACT_FINDING"}]),
        lint_contract({"max_steps": "many"}),
        validate_output_format_contract({"outputs": [{"kind": "json", "required_fields": ["a"], "fields": []}]}),
        validate_plan_contract(plan={}, contract={"artifacts": {"required": [{"path": "out.md"}]}}),
        validate_run_trace_events(({"type": "state_transition", "from": "RUNNING", "to": "DONE"},)),
        validate_compact_resume_bundle({"pre_compact": {"current_status": "WAITING_FOR_TOOL"}, "compact": {}}),
        validate_tool_events(({"type": "tool_result", "result": None},)),
        validate_tool_guardrail_events(({"type": "tool_result", "tool": "read", "result": {}},)),
        validate_memory_skill_contract({"memory_writes": [{"memory_ref": "m1"}]}),
    ]:
        assert result.ok is False
        _assert_recovery(result)


def test_runtime_contract_validation_failures_return_recovery() -> None:
    snapshot = build_effective_contract_snapshot(run_id="run-1", layers=({"artifacts": {"required": []}},))
    for result in [
        validate_concurrency_events((
            {"type": "lease_claimed", "run_id": "run-1", "worker_id": "a"},
            {"type": "lease_claimed", "run_id": "run-1", "worker_id": "b"},
        )),
        validate_subagent_contract({"parent": {"run_id": "p", "status": "DONE", "required_child_run_ids": ["c"]}}),
        validate_security_boundary_events(({"type": "network_request", "url": "file:///etc/passwd"},)),
        validate_verifier_integrity({"verifier": {"requires_llm": True}}),
        validate_runtime_card_set([RuntimeCard(kind="task", card_id="task-1", owner_user_id="u", status="RUNNING")]),
        validate_runtime_config({}),
        validate_replay_effective_contract({"contract_hash": "sha256:old"}, snapshot),
    ]:
        assert result.ok is False
        _assert_recovery(result)


def test_acceptance_reports_include_recovery_when_rejected() -> None:
    reports = [
        ArtifactAcceptanceReport(
            ok=False,
            artifact_ref="outputs/missing.md",
            findings=[ArtifactFinding("ARTIFACT_MISSING", "hard", "missing")],
        ),
        AcceptanceResult(
            ok=False,
            status="rejected",
            findings=[{"code": "ACCEPTANCE_ARTIFACTS_FAILED", "ok": False, "severity": "hard"}],
        ),
        EvidenceContractReport(ok=False, summary={}, findings=[{"code": "EVIDENCE_SOURCE_UNREADABLE"}]),
        TaskRunAcceptanceReport(
            ok=False,
            summary={},
            report_ref="acceptance.json",
            runtime_findings=[{"code": "STAGED_CHECKPOINT_MISSING"}],
        ),
        RealTaskAcceptanceReport(
            ok=False,
            summary={},
            report_ref="acceptance.json",
            runtime_findings=[{"code": "STAGED_CHECKPOINT_MISSING"}],
        ),
    ]

    for report in reports:
        _assert_recovery(report)
