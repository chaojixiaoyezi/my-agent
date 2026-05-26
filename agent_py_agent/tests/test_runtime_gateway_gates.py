from __future__ import annotations

from pathlib import Path

from agent_py_agent.agent.contracts.gates import (
    ApprovalBindingFacts,
    IdempotencyLedgerFacts,
    IdempotencyLedgerRecord,
    PathUrlCommandFacts,
    StateEventLedgerSnapshot,
    ToolEffectFacts,
    ToolGatePolicy,
    ToolManifestFacts,
    evaluate_approval_binding_gate,
    evaluate_idempotency_ledger_gate,
    evaluate_path_url_command_gate,
    evaluate_state_event_ledger_gate,
    evaluate_tool_call_gate,
    evaluate_tool_effect_gate,
    evaluate_tool_manifest_gate,
)


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

    assert missing_effect.finding_codes == ("TOOL_EFFECT_MISSING",)
    assert missing_idempotency.finding_codes == ("TOOL_IDEMPOTENCY_KEY_MISSING",)
    assert passed.allowed is True


def test_tool_effect_gate_requires_approval_for_dangerous_real_actions():
    dry_run = evaluate_tool_effect_gate(
        ToolEffectFacts("controlled_exec", effect="dangerous", mode="dry_run", idempotency_key="idem-shell-plan")
    )
    real_without_approval = evaluate_tool_effect_gate(
        ToolEffectFacts("controlled_exec", effect="dangerous", mode="real", idempotency_key="idem-shell-run")
    )
    real_with_approval = evaluate_tool_effect_gate(
        ToolEffectFacts(
            "controlled_exec",
            effect="dangerous",
            mode="real",
            idempotency_key="idem-shell-run",
            approval_id="approval-1",
        )
    )

    assert dry_run.allowed is True
    assert real_without_approval.status == "NEED_APPROVAL"
    assert real_without_approval.finding_codes == ("APPROVAL_REQUIRED",)
    assert real_with_approval.allowed is True


def test_tool_manifest_gate_requires_effect_schema_and_idempotency_contract():
    missing_effect = evaluate_tool_manifest_gate(ToolManifestFacts("write_file", parameters={"path": "string"}))
    missing_schema = evaluate_tool_manifest_gate(ToolManifestFacts("write_file", effect="mutating"))
    missing_policy = evaluate_tool_manifest_gate(
        ToolManifestFacts("write_file", effect="mutating", parameters={"path": "string"})
    )
    passed = evaluate_tool_manifest_gate(
        ToolManifestFacts("write_file", effect="mutating", parameters={"path": "string"}, requires_idempotency=True)
    )

    assert missing_effect.finding_codes == ("TOOL_MANIFEST_EFFECT_MISSING",)
    assert missing_schema.finding_codes == ("TOOL_MANIFEST_SCHEMA_MISSING",)
    assert missing_policy.finding_codes == ("TOOL_MANIFEST_IDEMPOTENCY_POLICY_MISSING",)
    assert passed.allowed is True


def test_tool_call_gate_applies_side_effect_policy_from_structured_facts():
    decision = evaluate_tool_call_gate(
        {"tool": "controlled_exec", "command": "pwd", "apply": True, "idempotency_key": "idem-real"},
        available_tools={"controlled_exec"},
        allowed_tools=["controlled_exec"],
        policy=ToolGatePolicy(tool_effects={"controlled_exec": "dangerous"}),
    )

    assert decision.status == "NEED_APPROVAL"
    assert decision.finding_codes == ("APPROVAL_REQUIRED",)


def test_path_url_command_gate_blocks_escape_private_url_and_shell_operators(tmp_path: Path):
    workspace = _workspace_with_symlink_escape(tmp_path)

    path_escape = _path_gate({"tool": "read_file", "path": "link"}, workspace)
    private_url = _path_gate({"tool": "fetch_url", "url": "http://127.1/admin"}, workspace)
    file_url = _path_gate({"tool": "fetch_url", "url": "file:///etc/passwd"}, workspace)
    command_operator = _path_gate({"tool": "run_command", "command": "python build.py && python test.py"}, workspace)
    passed = _path_gate({"tool": "run_command", "command": ["python3", "--version"], "working_dir": "."}, workspace)

    assert path_escape.finding_codes == ("PATH_SYMLINK_ESCAPE_BLOCKED",)
    assert private_url.finding_codes == ("NETWORK_PRIVATE_HOST_BLOCKED",)
    assert file_url.finding_codes == ("NETWORK_FILE_URL_BLOCKED",)
    assert command_operator.finding_codes == ("COMMAND_SHELL_OPERATOR_BLOCKED",)
    assert passed.allowed is True


def test_path_url_command_gate_blocks_argv_catastrophic_executable(tmp_path: Path):
    decision = _path_gate({"tool": "run_command", "argv": ["mkfs.ext4", "/dev/sda1"]}, tmp_path)

    assert decision.finding_codes == ("COMMAND_DANGEROUS_EXECUTABLE_BLOCKED",)
    assert decision.findings[0].evidence["executable"] == "mkfs.ext4"


def test_path_url_command_gate_blocks_string_dangerous_pattern(tmp_path: Path):
    decision = _path_gate({"tool": "run_command", "command": "sudo rm -rf /etc"}, tmp_path)

    assert decision.finding_codes == ("COMMAND_DANGEROUS_PATTERN_BLOCKED",)
    assert decision.findings[0].evidence["pattern"] == "RM_PROTECTED_TARGET"


def test_path_url_command_gate_allows_read_only_command(tmp_path: Path):
    decision = _path_gate({"tool": "run_command", "command": "git status --short"}, tmp_path)

    assert decision.allowed is True


def test_approval_binding_gate_rejects_mismatched_args_hash_or_run():
    approved = _approval_record()
    wrong_args = evaluate_approval_binding_gate(_approval_facts(approved, args_hash="sha256:def"))
    wrong_run = evaluate_approval_binding_gate(_approval_facts(approved, run_id="run-2"))
    passed = evaluate_approval_binding_gate(_approval_facts(approved))

    assert wrong_args.finding_codes == ("APPROVAL_BINDING_MISMATCH",)
    assert wrong_run.finding_codes == ("APPROVAL_BINDING_MISMATCH",)
    assert passed.allowed is True
    assert passed.evidence["approval_id"] == "approval-1"


def test_idempotency_ledger_gate_blocks_duplicate_or_changed_side_effects():
    duplicate = evaluate_idempotency_ledger_gate(_idempotency_facts("idem-1", "sha256:abc", "op-2"))
    changed_args = evaluate_idempotency_ledger_gate(_idempotency_facts("idem-1", "sha256:def", "op-3"))
    first_time = evaluate_idempotency_ledger_gate(
        IdempotencyLedgerFacts("write_file", "mutating", "idem-2", "sha256:abc", "op-4")
    )

    assert duplicate.status == "BLOCKED"
    assert duplicate.finding_codes == ("IDEMPOTENCY_REPLAY_REUSE_PREVIOUS_RESULT",)
    assert changed_args.finding_codes == ("IDEMPOTENCY_ARGS_HASH_MISMATCH",)
    assert first_time.allowed is True


def test_state_event_ledger_gate_requires_events_and_blocks_late_or_duplicate_actions():
    missing_event = evaluate_state_event_ledger_gate(
        StateEventLedgerSnapshot("run-1", "RUNNING", transitions=[{"from_status": "PLANNING", "to_status": "RUNNING"}])
    )
    done_dispatch = evaluate_state_event_ledger_gate(
        StateEventLedgerSnapshot(
            "run-1",
            "DONE",
            next_action="dispatch",
            events=[{"event_id": "evt-1", "run_id": "run-1", "event_type": "state_transition"}],
        )
    )
    expired_lease_tool_result = evaluate_state_event_ledger_gate(_expired_lease_snapshot())
    passed = evaluate_state_event_ledger_gate(
        StateEventLedgerSnapshot(
            "run-1",
            "VERIFYING",
            transitions=[{"from_status": "RUNNING", "to_status": "VERIFYING", "event_id": "evt-1"}],
            events=[{"event_id": "evt-1", "run_id": "run-1", "event_type": "state_transition"}],
        )
    )

    assert missing_event.finding_codes == ("STATE_EVENT_LEDGER_EVENT_MISSING",)
    assert done_dispatch.finding_codes == ("STATE_EVENT_LEDGER_TERMINAL_ACTION_BLOCKED",)
    assert expired_lease_tool_result.finding_codes == ("STATE_EVENT_LEDGER_LEASE_EXPIRED_RESULT",)
    assert passed.allowed is True


def _workspace_with_symlink_escape(tmp_path: Path) -> Path:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("secret", encoding="utf-8")
    (workspace / "link").symlink_to(outside)
    return workspace


def _path_gate(payload: dict[str, object], workspace: Path):
    return evaluate_path_url_command_gate(PathUrlCommandFacts(payload, workspace, workspace_roots=[workspace]))


def _approval_record() -> dict[str, object]:
    return {
        "approval_id": "approval-1",
        "status": "APPROVED",
        "tool": "controlled_exec",
        "run_id": "run-1",
        "operation_id": "op-1",
        "idempotency_key": "idem-1",
        "args_hash": "sha256:abc",
    }


def _approval_facts(
    approved: dict[str, object],
    *,
    run_id: str = "run-1",
    args_hash: str = "sha256:abc",
) -> ApprovalBindingFacts:
    return ApprovalBindingFacts(
        "controlled_exec",
        run_id=run_id,
        operation_id="op-1",
        idempotency_key="idem-1",
        args_hash=args_hash,
        approved_actions=(approved,),
    )


def _idempotency_facts(key: str, args_hash: str, operation_id: str) -> IdempotencyLedgerFacts:
    return IdempotencyLedgerFacts(
        "write_file",
        "mutating",
        key,
        args_hash,
        operation_id,
        ledger_records=(
            IdempotencyLedgerRecord("idem-1", "sha256:abc", "op-1", "SUCCEEDED"),
        ),
    )


def _expired_lease_snapshot() -> StateEventLedgerSnapshot:
    return StateEventLedgerSnapshot(
        "run-1",
        "WAITING_FOR_TOOL",
        lease_status="EXPIRED",
        events=[
            {"event_id": "evt-1", "run_id": "run-1", "event_type": "state_transition"},
            {"event_id": "evt-2", "run_id": "run-1", "event_type": "tool_result", "operation_id": "op-1"},
        ],
    )
