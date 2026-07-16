from __future__ import annotations

from pathlib import Path

from agent_py_agent.agent.contracts.gates.adapters import (
    evaluate_state_transition_gate,
    evaluate_tool_call_gate,
)
from agent_py_agent.agent.contracts.gates.artifact_gate import (
    evaluate_artifact_report_gate,
)
from agent_py_agent.agent.contracts.gates.artifact_provenance import (
    evaluate_artifact_provenance_gate,
)
from agent_py_agent.agent.contracts.gates.models import GateContext, GateDecision, GateFinding
from agent_py_agent.agent.contracts.gates.registry import GateRegistry
from agent_py_agent.agent.contracts.gates.run_contract import evaluate_run_contract_gate
from agent_py_agent.agent.contracts.gates.runtime_reports import (
    evaluate_recovery_lineage_gate,
    evaluate_recovery_replay_gate,
    evaluate_runtime_audit_gate,
)
from agent_py_agent.agent.contracts.gates.state_event_ledger import evaluate_state_event_ledger_gate


def test_gate_registry_blocks_when_any_required_gate_denies():
    registry = GateRegistry()
    registry.register("tool_execution", lambda _: GateDecision.allow("first"))
    registry.register("tool_execution", lambda _: GateDecision.deny("second", "BROKEN_FACT"))

    decision = registry.evaluate(GateContext(phase="tool_execution", payload={"tool": "read_file"}))

    assert decision.allowed is False
    assert decision.status == "DENY"
    assert decision.finding_codes == ("BROKEN_FACT",)


def test_gate_decision_serializes_recovery_envelope_with_chinese_message():
    decision = GateDecision.repair(
        "delivery_quality",
        [GateFinding("METRIC_WINDOW_MISSING", message="请补充时间窗口", evidence={"field": "stars_delta"})],
    )
    payload = decision.to_dict()

    recovery = payload["recovery"]
    action = recovery["actions"][0]
    assert recovery["status"] == "repair_required"
    assert recovery["can_auto_repair"] is True
    assert recovery["next_status"] == "REPAIRING"
    assert recovery["finding_codes"] == ["METRIC_WINDOW_MISSING"]
    assert action["recommended_action"] == "repair_structured_checkpoint_json"
    assert action["evidence"]["field"] == "stars_delta"
    assert "请" in recovery["message_zh"]


def test_gate_decision_exposes_action_and_operator_semantics():
    warning = GateDecision(
        "tool_guardrail",
        "ALLOW",
        True,
        (GateFinding("TOOL_GUARDRAIL_REPEAT_FAILURE_HINT", "P1", "换个查询方式", {"path": "logs/a.json"}),),
        "change_strategy",
        {"artifact_refs": ["reports/a.md"], "nested": {"path": "logs/a.json"}},
    )
    terminal = GateDecision(
        "tool_guardrail",
        "DENY",
        False,
        (GateFinding("TOOL_GUARDRAIL_REPEAT_FAILURE_BLOCKED"),),
        "change_strategy",
        {"block_task": True},
    )

    payload = warning.to_dict()

    assert payload["allow_action"] is True
    assert payload["block_task"] is False
    assert payload["severity"] == "engineering"
    assert payload["model_message"] == "换个查询方式"
    assert payload["operator_message"] == "tool_guardrail:ALLOW:TOOL_GUARDRAIL_REPEAT_FAILURE_HINT"
    assert payload["evidence_refs"] == ["reports/a.md", "logs/a.json"]
    assert terminal.to_dict()["block_task"] is True


def test_task_progress_open_items_are_repairable_not_terminal():
    decision = GateDecision.repair(
        "task_progress_closeout",
        [GateFinding("TASK_PROGRESS_OPEN_ITEMS", message="继续读取 fragment-015")],
    )
    recovery = decision.to_dict()["recovery"]

    assert recovery["status"] == "repair_required"
    assert recovery["terminal"] is False


def test_source_fact_identifier_repair_is_not_terminal():
    decision = GateDecision.repair(
        "source_fact_consistency",
        [GateFinding("SOURCE_CODE_IDENTIFIER_NOT_READ", message="补源码证据")],
    )

    recovery = decision.to_dict()["recovery"]

    assert recovery["status"] == "repair_required"
    assert recovery["terminal"] is False
    assert recovery["can_auto_repair"] is True
    assert recovery["actions"][0]["retryable"] is True
    assert recovery["actions"][0]["recommended_action"] == "repair_evidence_refs"


def test_target_coverage_missing_is_repairable_not_terminal():
    decision = GateDecision.repair(
        "target_coverage",
        [GateFinding("TARGET_COVERAGE_MISSING", message="继续 read_file(start_line=501)")],
    )
    recovery = decision.to_dict()["recovery"]

    assert recovery["status"] == "repair_required"
    assert recovery["terminal"] is False
    assert recovery["can_auto_repair"] is True
    assert recovery["actions"][0]["recommended_action"] == "continue"


def test_gate_decision_recovery_envelope_marks_approval_as_user_input():
    decision = GateDecision.need_approval("tool_effect", evidence={"tool_name": "block_ip"})
    recovery = decision.to_dict()["recovery"]

    assert recovery["status"] == "needs_user_input"
    assert recovery["requires_user"] is True
    assert recovery["next_status"] == "WAITING_APPROVAL"
    assert recovery["actions"][0]["recommended_action"] == "request_user_input"


def test_tool_call_gate_accepts_runtime_tool_payload_after_structured_normalization():
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


def test_artifact_provenance_gate_repairs_missing_or_cross_run_tool_evidence():
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


def test_artifact_provenance_accepts_materialized_write_record_with_failed_post_validation(tmp_path: Path):
    artifact_dir = tmp_path / "site"
    artifact_dir.mkdir()
    (artifact_dir / "index.html").write_text("<!doctype html><html></html>", encoding="utf-8")
    decision = evaluate_artifact_provenance_gate(
        {
            "path": str(artifact_dir),
            "ok": True,
            "provenance": _artifact_provenance_from_archive_row(tmp_path, artifact_dir / "index.html"),
        },
        run_id="run-1",
    )

    assert decision.allowed is True


def test_artifact_provenance_prefers_latest_current_run_write_over_read(tmp_path: Path):
    from agent_py_agent.agent.contracts.gates.artifact_provenance import (
        artifact_provenance_from_archive,
    )

    artifact_path = tmp_path / "output" / "final_report.md"
    artifact_path.parent.mkdir()
    artifact_path.write_text("final report", encoding="utf-8")

    provenance = artifact_provenance_from_archive(
        {"path": str(artifact_path), "ok": True},
        [
            {
                "tool": "read_file",
                "ok": True,
                "run_id": "run-1",
                "call_id": "read-1",
                "created_at": "2026-06-08T02:25:26+00:00",
                "parameters": {"path": str(artifact_path.relative_to(tmp_path))},
                "runtime_gate": {
                    "allowed": True,
                    "status": "ALLOW",
                    "evidence": {
                        "tool_name": "read_file",
                        "operation_id": "op-read",
                        "idempotency_key": "idem-read",
                    },
                },
            },
            {
                "tool": "write_file",
                "ok": True,
                "run_id": "run-1",
                "call_id": "write-1",
                "created_at": "2026-06-08T02:40:00+00:00",
                "parameters": {"path": str(artifact_path.relative_to(tmp_path))},
                "runtime_gate": {
                    "allowed": True,
                    "status": "ALLOW",
                    "evidence": {
                        "tool_name": "write_file",
                        "operation_id": "op-write",
                        "idempotency_key": "idem-write",
                    },
                },
            },
        ],
        run_id="run-1",
        workspace_root=tmp_path,
    )

    assert provenance["tool_name"] == "write_file"
    assert provenance["created_at"] == "2026-06-08T02:40:00+00:00"


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
        {"previous_artifact_refs": [{"artifact_ref": "out.txt", "source_run_id": "", "operation_id": ""}]}
    )
    passed = evaluate_recovery_lineage_gate(
        {"previous_artifact_refs": [{"artifact_ref": "out.txt", "source_run_id": "run-old", "operation_id": "op-old"}]}
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


def _artifact_provenance_from_archive_row(root: Path, written_path: Path) -> dict[str, object]:
    from agent_py_agent.agent.contracts.gates.artifact_provenance import (
        artifact_provenance_from_archive,
    )

    return artifact_provenance_from_archive(
        {"path": str(written_path.parent), "ok": True},
        [
            {
                "tool": "write_file",
                "ok": False,
                "run_id": "run-1",
                "parameters": {"path": str(written_path.relative_to(root))},
                "runtime_gate": {
                    "allowed": True,
                    "status": "ALLOW",
                    "evidence": {
                        "tool_name": "write_file",
                        "operation_id": "op-write",
                        "idempotency_key": "idem-write",
                    },
                },
            }
        ],
        run_id="run-1",
        workspace_root=root,
    )
