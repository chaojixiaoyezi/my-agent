
from __future__ import annotations

import re

from .recovery_actions import RecoveryAction

_APPROVAL_WAIT_CODES = {"APPROVAL_REQUIRED", "APPROVAL_NOT_FOUND", "APPROVAL_PENDING"}


def action_status(status: str, code: str) -> str:
    upper_status = str(status or "").strip().upper()
    normalized_code = _normalized_code(code)
    if hard_stop_code(normalized_code):
        return "blocked"
    if normalized_code in _APPROVAL_WAIT_CODES or normalized_code == "TOOL_NOT_ALLOWED" or upper_status in {"NEED_APPROVAL", "WAITING_HUMAN"}:
        return "needs_user_input"
    if upper_status == "RECOVERING" or recovering_code(normalized_code):
        return "recovering"
    if upper_status in {"NEED_REPAIR", "DENY", "BLOCKED"} and repairable_code(normalized_code):
        return "repair_required"
    return "blocked"


def repairable_code(code: str) -> bool:
    code = _normalized_code(code)
    return code.startswith(
        (
            "ACCEPTANCE_",
            "ARTIFACT_",
            "BUILDER_",
            "COLLABORATION_",
            "COLLECTION_",
            "CSV_",
            "DELIVERY_",
            "DOCUMENT_",
            "DOCX_",
            "EVIDENCE_",
            "FACT_",
            "FINAL_CLOSEOUT_",
            "HTML_",
            "JSON_",
            "LANGUAGE_",
            "MARKDOWN_",
            "METRIC_",
            "PATH_",
            "PDF_",
            "SPREADSHEET_",
            "STAGED_",
            "STATE_TRANSITION_",
            "TASK_PROGRESS_",
            "TARGET_COVERAGE_",
            "STATIC_SITE_",
            "TOOL_PROTOCOL_",
            "TOOL_INVALID_",
            "TOOL_NOT_REGISTERED",
            "TOOL_MANIFEST_",
            "XLSX_",
        )
    ) or code in {
        "CLOSEOUT_ARTIFACTS_MISSING",
        "CLOSEOUT_REPORT_REF_MISSING",
        "CONTRACT_SCHEMA_INVALID",
        "EFFECTIVE_CONTRACT_ARTIFACTS_INVALID",
        "EFFECTIVE_CONTRACT_MISSING",
        "UNKNOWN_VERIFIER",
    }


def recovering_code(code: str) -> bool:
    code = _normalized_code(code)
    return code.startswith(("AUDIT_", "RECOVERY_", "RUNLOG_", "SCOPE_")) or code in {
        "REQUEST_ID_MISSING",
        "RUN_ID_MISSING",
        "STATE_CORRUPT",
        "TASK_ID_MISSING",
        "WORKSPACE_ROOT_MISSING",
        "EFFECTIVE_CONTRACT_HASH_MISSING",
        "EFFECTIVE_CONTRACT_REF_MISSING",
        "TOOL_MANIFEST_REF_MISSING",
        "REVALIDATE_ARTIFACT_BEFORE_RERUN",
    }


def hard_stop_code(code: str) -> bool:
    code = _normalized_code(code)
    return code in {
        "APPROVAL_ALREADY_USED",
        "APPROVAL_BINDING_MISMATCH",
        "APPROVAL_EXPIRED",
        "APPROVAL_REJECTED",
        "APPROVER_NOT_AUTHORIZED",
        "STATE_CHECKSUM_MISMATCH",
        "USER_CANCELLED",
    }


def recovery_category(code: str) -> str:
    code = _normalized_code(code)
    if code.startswith(("ARTIFACT_", "BUILDER_", "DOCUMENT_", "DOCX_", "HTML_", "XLSX_", "CSV_", "JSON_", "PDF_", "MARKDOWN_", "SPREADSHEET_", "STATIC_SITE_")):
        return "artifact"
    if code.startswith(("EVIDENCE_", "FACT_", "METRIC_", "LANGUAGE_", "COLLABORATION_", "COLLECTION_", "TASK_PROGRESS_", "TARGET_COVERAGE_")):
        return "evidence"
    if code.startswith(("TOOL_", "TOOL_PROTOCOL_")):
        return "tool"
    if code.startswith("PATH_"):
        return "path"
    if code.startswith(("APPROVAL_", "USER_")):
        return "approval"
    if code.startswith(("AUDIT_", "RUNLOG_", "RECOVERY_")):
        return "recovery"
    if code.startswith(("STATE_", "FINAL_CLOSEOUT_", "ACCEPTANCE_")):
        return "state"
    return "contract"


def recommended_action(code: str, status: str) -> str:
    code = _normalized_code(code)
    status = str(status or "").strip().lower()
    if status == "needs_user_input":
        return RecoveryAction.REQUEST_USER_INPUT.value
    if status == "recovering":
        return RecoveryAction.RECOVER_FROM_CHECKPOINT.value
    if status == "blocked":
        return RecoveryAction.REPORT_BLOCKER.value
    if code.startswith("COLLABORATION_"):
        return RecoveryAction.CONTINUE_COLLABORATION.value
    if code == "TARGET_COVERAGE_MISSING":
        return RecoveryAction.CONTINUE.value
    if code == "TASK_PROGRESS_OPEN_ITEMS":
        return RecoveryAction.CONTINUE.value
    if code.startswith("TASK_PROGRESS_"):
        return RecoveryAction.REPAIR_EVIDENCE_REFS.value
    if code.startswith(("METRIC_", "LANGUAGE_", "COLLECTION_")):
        return RecoveryAction.REPAIR_STRUCTURED_CHECKPOINT_JSON.value
    if code.startswith(("EVIDENCE_", "FACT_")):
        return RecoveryAction.REPAIR_EVIDENCE_REFS.value
    if code.startswith(("ARTIFACT_", "BUILDER_", "DOCUMENT_", "DOCX_", "HTML_", "XLSX_", "CSV_", "JSON_", "PDF_", "MARKDOWN_", "STATIC_SITE_")):
        return RecoveryAction.REPAIR_ARTIFACT_AGAINST_FINDINGS.value
    if code.startswith(("STAGED_", "SPREADSHEET_")):
        return RecoveryAction.REPAIR_STRUCTURED_CHECKPOINT_JSON.value
    if code.startswith("PATH_"):
        return RecoveryAction.FIX_PATH_WITHIN_ALLOWED_ROOTS.value
    if code.startswith("TOOL_PROTOCOL_"):
        return RecoveryAction.REPAIR_TOOL_CALL.value
    if code in {"TOOL_NOT_REGISTERED", "TOOL_MANIFEST_EFFECT_MISSING"} or code.startswith("TOOL_MANIFEST_"):
        return RecoveryAction.CHOOSE_REGISTERED_TOOL.value
    if code.startswith("TOOL_"):
        return RecoveryAction.REPAIR_TOOL_ARGUMENTS.value
    if code.startswith(("CONTRACT_", "EFFECTIVE_CONTRACT_", "UNKNOWN_VERIFIER")):
        return RecoveryAction.REPAIR_EFFECTIVE_CONTRACT.value
    if code.startswith(("STATE_", "FINAL_CLOSEOUT_", "ACCEPTANCE_")):
        return RecoveryAction.RERUN_ACCEPTANCE_AFTER_REPAIR.value
    return RecoveryAction.REPAIR_AGAINST_CONTRACT_FINDINGS.value


def next_status(status: str) -> str:
    return {
        "repair_required": "REPAIRING",
        "needs_user_input": "WAITING_APPROVAL",
        "recovering": "RECOVERING",
        "blocked": "BLOCKED",
    }.get(status, "BLOCKED")


def _normalized_code(code: str) -> str:
    text = str(code or "").strip().upper()
    if not re.match(r"^[A-Z0-9_-]+$", text):
        return text
    return text.replace("-", "_")


__all__ = [
    "action_status",
    "next_status",
    "recommended_action",
    "recovery_category",
]
