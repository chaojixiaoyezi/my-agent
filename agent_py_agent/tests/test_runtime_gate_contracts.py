from __future__ import annotations

from agent_py_agent.agent.contracts.gates import (
    GateContext,
    GateDecision,
    GateRegistry,
    evaluate_acceptance_closeout_gate,
    evaluate_artifact_provenance_gate,
    evaluate_artifact_report_gate,
    evaluate_delivery_closeout_gate,
    evaluate_final_closeout_gate,
    evaluate_recovery_lineage_gate,
    evaluate_recovery_replay_gate,
    evaluate_run_contract_gate,
    evaluate_runtime_audit_gate,
    evaluate_state_event_ledger_gate,
    evaluate_state_transition_gate,
    evaluate_tool_call_gate,
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
            "run_id": "run-1",
            "report_ref": "reports/delivery.json",
            "artifacts": [
                {
                    "artifact_id": "a1",
                    "ok": True,
                    "path": "out.txt",
                    "kind": "txt",
                    "provenance": _artifact_provenance("out.txt"),
                }
            ],
        }
    )

    assert missing_ref.allowed is False
    assert missing_ref.finding_codes == ("CLOSEOUT_REPORT_REF_MISSING",)
    assert failed_artifact.allowed is False
    assert failed_artifact.status == "NEED_REPAIR"
    assert passed.allowed is True


def test_run_contract_gate_requires_scope_and_records_effective_contract_hash():
    missing_scope = evaluate_run_contract_gate(
        {"case_id": "case-1", "artifacts": [{"artifact_id": "out", "path": "out.txt"}]},
        scope={"request_id": "req-1", "run_id": "", "task_id": "task-1", "workspace_root": "/tmp/work"},
    )
    passed = evaluate_run_contract_gate(
        {"case_id": "case-1", "artifacts": [{"artifact_id": "out", "path": "out.txt"}]},
        scope={"request_id": "req-1", "run_id": "run-1", "task_id": "task-1", "workspace_root": "/tmp/work"},
    )

    assert missing_scope.allowed is False
    assert missing_scope.finding_codes == ("RUN_ID_MISSING",)
    assert passed.allowed is True
    assert passed.evidence["effective_contract_hash"].startswith("sha256:")
    assert passed.evidence["artifact_count"] == 1


# LLM: Run contract gate must lint the effective contract before hashing it.
# 函数用途: 验证坏合同不能只因为有 hash 就进入执行/收口链路。
def test_run_contract_gate_rejects_contract_doctor_findings():
    decision = evaluate_run_contract_gate(
        {"version": 2, "artifact_path": "out.md", "rules": ["magic_verify"]},
        scope={
            "request_id": "req-1",
            "run_id": "run-1",
            "task_id": "task-1",
            "workspace_root": "/tmp/work",
        },
    )

    assert decision.allowed is False
    assert decision.finding_codes == ("CONTRACT_SCHEMA_INVALID", "UNKNOWN_VERIFIER")


def test_artifact_provenance_gate_requires_current_run_tool_evidence():
    missing = evaluate_artifact_provenance_gate({"path": "out.txt", "ok": True}, run_id="run-1")
    old_run = evaluate_artifact_provenance_gate(
        {"path": "out.txt", "ok": True, "provenance": _artifact_provenance("out.txt", run_id="run-old")},
        run_id="run-1",
    )
    passed = evaluate_artifact_provenance_gate(
        {"path": "out.txt", "ok": True, "provenance": _artifact_provenance("out.txt")},
        run_id="run-1",
    )

    assert missing.allowed is False
    assert missing.finding_codes == ("ARTIFACT_PROVENANCE_MISSING",)
    assert old_run.allowed is False
    assert old_run.finding_codes == ("ARTIFACT_PROVENANCE_RUN_MISMATCH",)
    assert passed.allowed is True


def test_final_closeout_gate_requires_run_artifact_state_and_acceptance_gates():
    missing_child_gate = evaluate_final_closeout_gate(
        {
            "run_contract_gate": {"allowed": True, "status": "ALLOW"},
            "runtime_gate": {"allowed": True, "status": "ALLOW"},
            "delivery_quality_gate": {"allowed": True, "status": "ALLOW"},
            "acceptance_gate": {"allowed": True, "status": "ALLOW"},
        }
    )
    passed = evaluate_final_closeout_gate(
        {
            "run_contract_gate": {"allowed": True, "status": "ALLOW"},
            "runtime_gate": {"allowed": True, "status": "ALLOW"},
            "state_gate": {"allowed": True, "status": "ALLOW"},
            "acceptance_gate": {"allowed": True, "status": "ALLOW"},
            "delivery_quality_gate": {"allowed": True, "status": "ALLOW"},
        }
    )

    assert missing_child_gate.allowed is False
    assert missing_child_gate.finding_codes == ("FINAL_CLOSEOUT_STATE_GATE_MISSING",)
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


def test_recovery_lineage_gate_requires_explicit_previous_artifact_refs():
    missing = evaluate_recovery_lineage_gate(
        {"previous_artifact_refs": [{"path": "out.txt", "source_run_id": "", "operation_id": ""}]}
    )
    passed = evaluate_recovery_lineage_gate(
        {"previous_artifact_refs": [{"path": "out.txt", "source_run_id": "run-old", "operation_id": "op-old"}]}
    )

    assert missing.allowed is False
    assert missing.finding_codes == ("RECOVERY_ARTIFACT_LINEAGE_MISSING",)
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


# LLM: Runtime state gate must reject events that belong to another run.
# 函数用途: 验证旧 run 的异步工具/审批事件不会推进当前 run 状态。
def test_state_event_ledger_gate_rejects_cross_run_events():
    decision = evaluate_state_event_ledger_gate(
        {
            "run_id": "run-2",
            "current_status": "RUNNING",
            "events": [
                {"event_id": "e1", "run_id": "run-1", "event_type": "tool_result", "operation_id": "op-1"}
            ],
        }
    )

    assert decision.allowed is False
    assert decision.finding_codes == ("STATE_EVENT_LEDGER_RUN_MISMATCH",)


def _artifact_provenance(path: str, *, run_id: str = "run-1") -> dict[str, object]:
    return {
        "ok": True,
        "artifact_ref": path,
        "run_id": run_id,
        "tool_name": "write_file",
        "operation_id": "op-write-1",
        "idempotency_key": "idem-write-1",
        "created_by_current_run": True,
    }
