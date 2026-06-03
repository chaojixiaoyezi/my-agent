
from __future__ import annotations

from pathlib import Path


def build_small_llm_canary_gate(workspace: Path) -> dict[str, object]:
    root = Path(workspace).expanduser().resolve()
    return {
        "cases": [
            _canary_case(root, "llm_canary_file_artifact", ("read_only", "dry_run"), ("output.md",)),
            _canary_case(root, "llm_canary_tool_failure_repair", ("read_only",), ("repair_report.md",)),
            _canary_case(root, "llm_canary_compact_resume", ("read_only", "dry_run"), ("resume_state.json",)),
            _canary_case(root, "llm_canary_static_web_artifact", ("read_only", "dry_run"), ("index.html",)),
        ],
    }


def model_adapter_facts() -> dict[str, object]:
    return {
        "tool_calls": [
            {"tool_name": "read_file", "tool_call_id": "provider-call-1"},
            {"tool_name": "write_file", "generated_tool_call_id": "generated-call-2"},
        ],
        "stream": {"complete": True, "partial_json": False},
        "retry_limit": 2,
        "model_errors": [{"code": "rate_limit", "retryable": True}],
        "model_switch": {"from_schema": "tool_protocol_v2", "to_schema": "tool_protocol_v2"},
    }


def prompt_context_facts() -> dict[str, object]:
    required = (
        "contract_hash",
        "allowed_tools",
        "required_artifacts",
        "acceptance_contract_ref",
        "run_scope_ref",
        "tool_manifest_ref",
    )
    return {
        "required_contract_fields": required,
        "assembled_context": {
            "contract_hash": "sha256:activation-contract",
            "allowed_tools": ("read_file", "write_file"),
            "required_artifacts": ({"path": "artifacts/output.md"},),
            "acceptance_contract_ref": "acceptance://activation",
            "run_scope_ref": "runscope://activation",
            "tool_manifest_ref": "toolmanifest://activation",
        },
        "truncated_fields": (),
        "allowed_tools": ("read_file", "write_file"),
        "tool_schemas": {
            "read_file": {"input": {"path": "string"}},
            "write_file": {"input": {"path": "string", "content_ref": "string"}},
        },
        "untrusted_inputs": [{"source": "user", "applied_to_contract": False}],
        "memory_entries": [{"entry_ref": "memory://preference", "attempted_contract_override": False}],
    }


def side_effect_events() -> tuple[dict[str, object], ...]:
    return (
        {"type": "tool_registration", "tool": "read_file", "effect": "read_only"},
        {"type": "tool_registration", "tool": "write_file", "effect": "mutating"},
        {"type": "tool_call", "tool": "read_file", "effect": "read_only", "wrote_paths": []},
        {
            "type": "tool_call",
            "tool": "write_file",
            "effect": "mutating",
            "idempotency_key": "idem-write-activation",
            "replay_mode": False,
            "executed": True,
        },
        {"type": "tool_result", "tool": "write_file", "mode": "dry_run", "claimed_real_execution": False},
    )


def trace_events() -> tuple[dict[str, object], ...]:
    return (
        {"type": "state_transition", "run_id": "run-activation", "from": "PLANNING", "event": "PLAN_DONE", "to": "RUNNING"},
        {
            "type": "state_transition",
            "run_id": "run-activation",
            "from": "RUNNING",
            "event": "NEED_TOOL",
            "to": "WAITING_FOR_TOOL",
        },
        {
            "type": "tool_result",
            "run_id": "run-activation",
            "tool": "read_file",
            "operation_id": "op-read-1",
            "ok": True,
            "duration_ms": 7,
        },
        {
            "type": "state_transition",
            "run_id": "run-activation",
            "from": "WAITING_FOR_TOOL",
            "event": "TOOL_OK",
            "to": "RUNNING",
        },
        {"type": "state_transition", "run_id": "run-activation", "from": "RUNNING", "event": "TASK_DONE", "to": "VERIFYING"},
        {"type": "state_transition", "run_id": "run-activation", "from": "VERIFYING", "event": "VERIFY_PASS", "to": "DONE"},
    )


def trace_capture_manifest() -> dict[str, object]:
    return {
        "refs_only": True,
        "redaction_ok": True,
        "required_refs": (
            "llm_input_ref",
            "llm_output_ref",
            "tool_trace_ref",
            "state_events_ref",
            "artifact_refs",
            "acceptance_report_ref",
            "replay_spec_ref",
            "failure_sample_ref",
        ),
        "capture_refs": {
            "llm_input_ref": "llm-input://run-activation/turn-1",
            "llm_output_ref": "llm-output://run-activation/turn-1",
            "tool_trace_ref": "tooltrace://run-activation",
            "state_events_ref": "runlog://run-activation",
            "artifact_refs": ("artifact://run-activation/output.md",),
            "acceptance_report_ref": "acceptance://run-activation/report",
            "replay_spec_ref": "replay://run-activation/spec",
            "failure_sample_ref": "failure-sample://run-activation/if-failed",
        },
    }


def trace_manifest_issues(manifest: dict[str, object]) -> list[str]:
    issues: list[str] = []
    refs = manifest.get("capture_refs") if isinstance(manifest.get("capture_refs"), dict) else {}
    if manifest.get("refs_only") is not True:
        issues.append("TRACE_CAPTURE_NOT_REFS_ONLY")
    if manifest.get("redaction_ok") is not True:
        issues.append("TRACE_CAPTURE_REDACTION_MISSING")
    for key in _string_tuple(manifest.get("required_refs")):
        if key not in refs:
            issues.append("TRACE_CAPTURE_REF_MISSING")
            break
    return issues


def _canary_case(
    root: Path,
    case_id: str,
    tool_modes: tuple[str, ...],
    artifact_paths: tuple[str, ...],
) -> dict[str, object]:
    expected_artifacts = [{"path": path, "min_size": 1} for path in artifact_paths]
    return {
        "case_id": case_id,
        "complexity": "small",
        "workspace_ref": f"workspace://{root.name}/{case_id}",
        "isolation_ok": True,
        "real_execution_allowed": False,
        "allowed_effects": tuple("dry_run" if mode == "dry_run" else "read_only" for mode in tool_modes),
        "tool_modes": tool_modes,
        "prompt_ref": f"prompt://llm-canary/{case_id}",
        "expected_artifacts": expected_artifacts,
        "verification_refs": (f"acceptance://llm-canary/{case_id}",),
        "replay_capture_enabled": True,
        "max_runtime_seconds": 600,
    }


def _string_tuple(value: object) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple, set)):
        return ()
    return tuple(text for item in value for text in (str(item or "").strip(),) if text)


__all__ = [
    "build_small_llm_canary_gate",
    "model_adapter_facts",
    "prompt_context_facts",
    "side_effect_events",
    "trace_capture_manifest",
    "trace_events",
    "trace_manifest_issues",
]
