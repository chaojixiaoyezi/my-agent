from __future__ import annotations

from agent_py_agent.agent.contracts.gates import (
    GateContext,
    GateDecision,
    GateRegistry,
    ToolEffectFacts,
    ToolGatePolicy,
    evaluate_acceptance_closeout_gate,
    evaluate_artifact_report_gate,
    evaluate_delivery_closeout_gate,
    evaluate_recovery_replay_gate,
    evaluate_runtime_audit_gate,
    evaluate_state_transition_gate,
    evaluate_tool_call_gate,
    evaluate_tool_effect_gate,
)


def test_gate_registry_blocks_when_any_required_gate_denies():
    registry = GateRegistry()
    registry.register("tool_execution", lambda _: GateDecision.allow("first"))
    registry.register("tool_execution", lambda _: GateDecision.deny("second", "BROKEN_FACT"))

    decision = registry.evaluate(GateContext(phase="tool_execution", payload={"tool": "read_file"}))

    assert decision.allowed is False
    assert decision.status == "DENY"
    assert decision.finding_codes == ("BROKEN_FACT",)


def test_tool_call_gate_accepts_legacy_payload_only_after_structured_normalization():
    decision = evaluate_tool_call_gate(
        {"tool": "read_file", "path": "README.md"},
        available_tools={"read_file"},
        allowed_tools=["read_file"],
    )

    assert decision.allowed is True
    assert decision.status == "ALLOW"
    assert decision.evidence["tool_name"] == "read_file"
    assert decision.evidence["operation_id"]
    assert decision.evidence["idempotency_key"]


def test_tool_call_gate_rejects_unknown_or_unauthorized_tools_before_execution():
    unknown = evaluate_tool_call_gate({"tool": "magic_tool"}, available_tools={"read_file"})
    unauthorized = evaluate_tool_call_gate(
        {"tool": "write_file", "path": "out.md", "content": "x"},
        available_tools={"write_file"},
        allowed_tools=["read_file"],
    )

    assert unknown.allowed is False
    assert unknown.finding_codes == ("TOOL_NOT_REGISTERED",)
    assert unauthorized.allowed is False
    assert unauthorized.finding_codes == ("TOOL_NOT_ALLOWED",)


def test_tool_effect_gate_requires_effect_and_idempotency_for_side_effects():
    missing_effect = evaluate_tool_effect_gate(ToolEffectFacts(tool_name="write_file"))
    missing_idempotency = evaluate_tool_effect_gate(
        ToolEffectFacts(tool_name="write_file", effect="mutating", mode="real")
    )
    passed = evaluate_tool_effect_gate(
        ToolEffectFacts(
            tool_name="write_file",
            effect="mutating",
            mode="real",
            idempotency_key="idem-write-1",
        )
    )

    assert missing_effect.allowed is False
    assert missing_effect.finding_codes == ("TOOL_EFFECT_MISSING",)
    assert missing_idempotency.allowed is False
    assert missing_idempotency.finding_codes == ("TOOL_IDEMPOTENCY_KEY_MISSING",)
    assert passed.allowed is True


def test_tool_effect_gate_requires_approval_for_dangerous_real_actions():
    dry_run = evaluate_tool_effect_gate(
        ToolEffectFacts(
            tool_name="controlled_exec",
            effect="dangerous",
            mode="dry_run",
            idempotency_key="idem-shell-plan",
        )
    )
    real_without_approval = evaluate_tool_effect_gate(
        ToolEffectFacts(
            tool_name="controlled_exec",
            effect="dangerous",
            mode="real",
            idempotency_key="idem-shell-run",
        )
    )
    real_with_approval = evaluate_tool_effect_gate(
        ToolEffectFacts(
            tool_name="controlled_exec",
            effect="dangerous",
            mode="real",
            idempotency_key="idem-shell-run",
            approval_id="approval-1",
        )
    )

    assert dry_run.allowed is True
    assert real_without_approval.allowed is False
    assert real_without_approval.status == "NEED_APPROVAL"
    assert real_without_approval.finding_codes == ("APPROVAL_REQUIRED",)
    assert real_with_approval.allowed is True


def test_tool_call_gate_applies_side_effect_policy_from_structured_facts():
    decision = evaluate_tool_call_gate(
        {
            "tool": "controlled_exec",
            "command": "pwd",
            "apply": True,
            "idempotency_key": "idem-controlled-exec-real",
        },
        available_tools={"controlled_exec"},
        allowed_tools=["controlled_exec"],
        policy=ToolGatePolicy(tool_effects={"controlled_exec": "dangerous"}),
    )

    assert decision.allowed is False
    assert decision.status == "NEED_APPROVAL"
    assert decision.finding_codes == ("APPROVAL_REQUIRED",)


def test_delivery_closeout_gate_requires_report_ref_and_passing_artifacts():
    missing_ref = evaluate_delivery_closeout_gate({"ok": True, "artifacts": []})
    failed_artifact = evaluate_delivery_closeout_gate(
        {
            "ok": False,
            "report_ref": "reports/delivery.json",
            "artifacts": [{"artifact_id": "a1", "ok": False}],
        }
    )
    passed = evaluate_delivery_closeout_gate(
        {
            "ok": True,
            "report_ref": "reports/delivery.json",
            "artifacts": [{"artifact_id": "a1", "ok": True, "path": "out.txt", "kind": "txt"}],
        }
    )

    assert missing_ref.allowed is False
    assert missing_ref.finding_codes == ("CLOSEOUT_REPORT_REF_MISSING",)
    assert failed_artifact.allowed is False
    assert failed_artifact.status == "NEED_REPAIR"
    assert passed.allowed is True


def test_artifact_report_gate_rejects_missing_refs_and_hard_findings():
    missing_ref = evaluate_artifact_report_gate({"ok": True, "artifact_kind": "txt"})
    hard_finding = evaluate_artifact_report_gate(
        {
            "ok": False,
            "artifact_ref": "out.txt",
            "artifact_kind": "txt",
            "findings": [{"code": "ARTIFACT_EMPTY", "severity": "hard"}],
        }
    )
    passed = evaluate_artifact_report_gate({"ok": True, "artifact_ref": "out.txt", "artifact_kind": "txt"})

    assert missing_ref.allowed is False
    assert missing_ref.finding_codes == ("ARTIFACT_REF_MISSING",)
    assert hard_finding.allowed is False
    assert hard_finding.status == "NEED_REPAIR"
    assert hard_finding.finding_codes == ("ARTIFACT_EMPTY",)
    assert passed.allowed is True


def test_state_transition_gate_forces_verification_before_done():
    direct_done = evaluate_state_transition_gate("RUNNING", "DONE")
    verifying = evaluate_state_transition_gate("RUNNING", "VERIFYING")
    accepted = evaluate_state_transition_gate("VERIFYING", "DONE")

    assert direct_done.allowed is False
    assert direct_done.finding_codes == ("STATE_TRANSITION_DISALLOWED",)
    assert verifying.allowed is True
    assert accepted.allowed is True


def test_acceptance_closeout_gate_requires_verified_runtime_gate():
    missing_runtime_gate = evaluate_acceptance_closeout_gate({"final_status": "DONE", "verification_status": "PASSED"})
    failed_runtime_gate = evaluate_acceptance_closeout_gate(
        {
            "final_status": "DONE",
            "verification_status": "PASSED",
            "runtime_gate": {"allowed": False, "findings": [{"code": "ARTIFACT_EMPTY"}]},
        }
    )
    passed = evaluate_acceptance_closeout_gate(
        {
            "final_status": "DONE",
            "verification_status": "PASSED",
            "runtime_gate": {"allowed": True, "status": "ALLOW"},
        }
    )

    assert missing_runtime_gate.allowed is False
    assert missing_runtime_gate.finding_codes == ("ACCEPTANCE_RUNTIME_GATE_MISSING",)
    assert failed_runtime_gate.allowed is False
    assert failed_runtime_gate.finding_codes == ("ARTIFACT_EMPTY",)
    assert passed.allowed is True


def test_recovery_replay_gate_requires_snapshot_scope_and_effective_contract():
    missing_contract = evaluate_recovery_replay_gate(
        {
            "run_id": "run-1",
            "task_id": "task-1",
            "scope": {"workspace_root": "/tmp/work"},
            "tool_manifest_ref": "refs/tool-manifest.json",
        }
    )
    passed = evaluate_recovery_replay_gate(
        {
            "run_id": "run-1",
            "task_id": "task-1",
            "scope": {"workspace_root": "/tmp/work"},
            "effective_contract_ref": "refs/effective-contract.json",
            "effective_contract_hash": "sha256:abc",
            "tool_manifest_ref": "refs/tool-manifest.json",
            "runlog_ref": "refs/runlog.jsonl",
        }
    )

    assert missing_contract.allowed is False
    assert missing_contract.finding_codes == (
        "EFFECTIVE_CONTRACT_REF_MISSING",
        "EFFECTIVE_CONTRACT_HASH_MISSING",
        "RUNLOG_REF_MISSING",
    )
    assert passed.allowed is True


def test_runtime_audit_gate_requires_tool_records_to_carry_gate_and_parameters():
    missing_gate = evaluate_runtime_audit_gate(
        [
            {
                "type": "tool_result",
                "tool": "write_file",
                "operation_id": "op-write",
                "parameters": {"path": "out.txt"},
            }
        ]
    )
    missing_parameters = evaluate_runtime_audit_gate(
        [
            {
                "type": "tool_result",
                "tool": "write_file",
                "operation_id": "op-write",
                "runtime_gate": {"status": "ALLOW", "allowed": True},
            }
        ]
    )
    passed = evaluate_runtime_audit_gate(
        [
            {
                "type": "tool_result",
                "tool": "write_file",
                "operation_id": "op-write",
                "parameters": {"path": "out.txt"},
                "runtime_gate": {"status": "ALLOW", "allowed": True},
            }
        ]
    )

    assert missing_gate.allowed is False
    assert missing_gate.finding_codes == ("AUDIT_RUNTIME_GATE_MISSING",)
    assert missing_parameters.allowed is False
    assert missing_parameters.finding_codes == ("AUDIT_PARAMETERS_MISSING",)
    assert passed.allowed is True
