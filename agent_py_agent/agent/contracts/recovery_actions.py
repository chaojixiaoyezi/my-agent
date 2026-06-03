from __future__ import annotations

from enum import Enum


class RecoveryAction(str, Enum):
    """Canonical recovery action vocabulary.

    Keep each value as one action. Branching choices such as whether to retry,
    take over, or stop belong in the surrounding decision payload and hint.
    """

    CHANGE_STRATEGY = "change_strategy"
    CHOOSE_PUBLIC_URL = "choose_public_url"
    CHOOSE_REGISTERED_TOOL = "choose_registered_tool"
    CHOOSE_VALID_PUBLIC_URL = "choose_valid_public_url"
    CLOSEOUT = "closeout"
    COLLECT_NON_EMPTY_ROWS = "collect_non_empty_rows"
    CONTINUE_COLLABORATION = "continue_collaboration"
    CONTINUE = "continue"
    DISPATCH = "dispatch"
    EXECUTE = "execute"
    FALLBACK_TO_CHECKPOINT = "fallback_to_checkpoint"
    FIX_PATH = "fix_path"
    FIX_PATH_WITHIN_ALLOWED_ROOTS = "fix_path_within_allowed_roots"
    MANUAL_REVIEW = "manual_review"
    MATERIALIZE_CHECKPOINT = "materialize_checkpoint"
    NONE = "none"
    READ_ARTIFACT_REF = "read_artifact_ref"
    RECOVER_FROM_CHECKPOINT = "recover_from_checkpoint"
    RECORD_FACT_EVIDENCE_PAYLOAD = "record_fact_evidence_payload_when_available"
    RECORD_PROVENANCE = "record_provenance_when_available"
    RECORD_QUALITY_PAYLOAD = "record_quality_payload_when_available"
    REGISTER_GATE_PIPELINE = "register_gate_pipeline"
    REGISTER_REQUIRED_GATE = "register_required_gate"
    REPAIR = "repair"
    REPAIR_AGAINST_ACCEPTANCE_FINDINGS = "repair_against_acceptance_findings"
    REPAIR_AGAINST_CONTRACT_FINDINGS = "repair_against_contract_findings"
    REPAIR_ARTIFACT_AGAINST_FINDINGS = "repair_artifact_against_findings"
    REPAIR_CHANNEL = "repair_channel"
    REPAIR_EFFECTIVE_CONTRACT = "repair_effective_contract"
    REPAIR_EVIDENCE_REFS = "repair_evidence_refs"
    REPAIR_STRUCTURED_CHECKPOINT_JSON = "repair_structured_checkpoint_json"
    REPAIR_STRUCTURED_SOURCE_JSON = "repair_structured_source_json"
    REPAIR_DOCUMENT_ARTIFACT = "repair_document_artifact"
    REPAIR_GATE_PIPELINE_ORDER = "repair_gate_pipeline_order"
    REPAIR_TOOL_ARGUMENTS = "repair_tool_arguments"
    REPAIR_TOOL_CALL = "repair_tool_call"
    REPAIR_TOOL_CALL_IDENTITY = "repair_tool_call_identity"
    REPAIR_URL_HOST = "repair_url_host"
    REPORT_BLOCKER = "report_blocker"
    REQUEST_APPROVAL = "request_approval"
    REQUEST_CAPABILITY = "request_capability"
    REQUEST_PERMISSION = "request_permission"
    REQUEST_USER_INPUT = "request_user_input"
    REUSE_PREVIOUS_RESULT = "reuse_previous_result"
    RERUN_ACCEPTANCE_AFTER_REPAIR = "rerun_acceptance_after_repair"
    RESUME_CASE = "resume_case"
    RETRY = "retry"
    RETRY_AFTER_BACKOFF = "retry_after_backoff"
    REWRITE_CHECKPOINT = "rewrite_checkpoint"
    STOP = "stop"
    SWITCH_BACKEND = "switch_backend"
    TAKEOVER = "takeover"
    USE_HTTP_URL = "use_http_url"
    VERIFY_CROSS_RUN_ARTIFACT = "verify_cross_run_artifact_when_needed"
    WAIT = "wait"
    WAIT_FOR_ACCEPTANCE = "wait_for_acceptance"
    WAIT_FOR_EXISTING_OPERATION = "wait_for_existing_operation"
    WAIT_FOR_LOCAL_PROGRESS = "wait_for_local_progress"
    REVIEW_FACT_EVIDENCE_FINDINGS = "review_fact_evidence_findings"
    REVIEW_QUALITY_FINDINGS = "review_quality_findings"
    REWRITE_ARTIFACT_BYTES = "rewrite_artifact_bytes"
    WRITE_MARKDOWN_SOURCE = "write_markdown_source"
    WRITE_NON_EMPTY_MARKDOWN_SOURCE = "write_non_empty_markdown_source"
    WRITE_NON_EMPTY_STRUCTURED_ROWS = "write_non_empty_structured_rows"
    WRITE_STRUCTURED_SOURCE_DATA = "write_structured_source_data"
    WRITE_TARGET_ARTIFACT = "write_target_artifact"


RECOVERY_ACTIONS = tuple(action.value for action in RecoveryAction)


def recovery_action_value(action: RecoveryAction | str) -> str:
    """Return a canonical action value or raise for unknown recovery actions."""
    if isinstance(action, RecoveryAction):
        return action.value
    value = str(action or "").strip()
    if value in known_recovery_action_values():
        return value
    raise ValueError(f"unknown recovery action: {value!r}")


def known_recovery_action_values() -> tuple[str, ...]:
    """Return every canonical recovery action value."""
    return tuple(sorted(RECOVERY_ACTIONS))


__all__ = [
    "RECOVERY_ACTIONS",
    "RecoveryAction",
    "known_recovery_action_values",
    "recovery_action_value",
]
