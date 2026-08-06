from __future__ import annotations

from pathlib import Path

from agent_py_agent.agent.contracts.gates.path_url_command import (
    PathUrlCommandFacts,
    evaluate_path_url_command_gate,
)
from agent_py_agent.agent.contracts.gates.state_event_ledger import (
    StateEventLedgerSnapshot,
    evaluate_state_event_ledger_gate,
)
from agent_py_agent.agent.contracts.gates.tool_approval_binding import (
    ApprovalBindingFacts,
    evaluate_approval_binding_gate,
)


def test_path_url_command_gate_blocks_escape_private_url_and_shell_operators(tmp_path: Path):
    workspace = _workspace_with_symlink_escape(tmp_path)

    path_escape = _path_gate({"tool": "read_file", "path": "link"}, workspace)
    private_url = _path_gate({"tool": "web_fetch", "url": "http://127.1/admin"}, workspace)
    file_url = _path_gate({"tool": "web_fetch", "url": "file:///etc/passwd"}, workspace)
    command_operator = _path_gate({"tool": "run_command", "command": "python build.py && python test.py"}, workspace)
    passed = _path_gate({"tool": "run_command", "command": ["python3", "--version"], "working_dir": "."}, workspace)

    assert path_escape.finding_codes == ("PATH_SYMLINK_ESCAPE_BLOCKED",)
    assert private_url.finding_codes == ("NETWORK_PRIVATE_HOST_BLOCKED",)
    assert file_url.finding_codes == ("NETWORK_FILE_URL_BLOCKED",)
    assert command_operator.finding_codes == ("COMMAND_SHELL_OPERATOR_BLOCKED",)
    assert passed.allowed is True


def test_path_url_command_gate_allows_only_declared_owner_scoped_file_url(tmp_path: Path):
    owner = tmp_path / "owners" / "user-a"
    owner.mkdir(parents=True)
    source = owner / "events.jsonl"
    source.write_text("{}\n", encoding="utf-8")

    allowed = evaluate_path_url_command_gate(
        PathUrlCommandFacts(
            payload={"tool": "watch_stream", "url": source.as_uri()},
            workspace_root=owner,
            workspace_roots=[owner],
            owner_scope_root=str(owner),
            local_file_url_fields=("url",),
        )
    )
    undeclared = _path_gate(
        {"tool": "web_fetch", "url": source.as_uri()},
        owner,
    )
    dangerous = evaluate_path_url_command_gate(
        PathUrlCommandFacts(
            payload={"tool": "watch_stream", "url": "file:///etc/passwd"},
            workspace_root=owner,
            workspace_roots=[owner],
            owner_scope_root=str(owner),
            local_file_url_fields=("url",),
        )
    )

    assert allowed.allowed is True
    assert undeclared.finding_codes == ("NETWORK_FILE_URL_BLOCKED",)
    assert dangerous.finding_codes == ("PATH_OWNER_SCOPE_BLOCKED",)


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
    assert missing_event.recommended_action == "repair"
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
        "tool_name": "controlled_exec",
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
