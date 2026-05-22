# LLM: Recovery classification maps stable finding codes to repair/user/block actions.
# 模块用途: 只按 code/status 分类合同失败，不读取普通自然语言 message。

from __future__ import annotations


def action_status(status: str, code: str) -> str:
    upper_status = str(status or "").strip().upper()
    if hard_stop_code(code):
        return "blocked"
    if code.startswith("APPROVAL_") or code == "TOOL_NOT_ALLOWED" or upper_status in {"NEED_APPROVAL", "WAITING_HUMAN"}:
        return "needs_user_input"
    if upper_status == "RECOVERING" or recovering_code(code):
        return "recovering"
    if upper_status in {"NEED_REPAIR", "DENY", "BLOCKED"} and repairable_code(code):
        return "repair_required"
    return "blocked"


def repairable_code(code: str) -> bool:
    return code.startswith(
        (
            "ACCEPTANCE_",
            "ARTIFACT_",
            "COLLECTION_",
            "CSV_",
            "DELIVERY_",
            "EVIDENCE_",
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
    return code in {"APPROVAL_REJECTED", "USER_CANCELLED", "STATE_CHECKSUM_MISMATCH"}


def recovery_category(code: str) -> str:
    if code.startswith(("ARTIFACT_", "HTML_", "XLSX_", "CSV_", "JSON_", "PDF_", "MARKDOWN_", "SPREADSHEET_", "STATIC_SITE_")):
        return "artifact"
    if code.startswith(("EVIDENCE_", "METRIC_", "LANGUAGE_", "COLLECTION_")):
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
    if status == "needs_user_input":
        return "request_user_input_or_approval"
    if status == "recovering":
        return "recover_from_checkpoint"
    if status == "blocked":
        return "stop_and_report_blocker"
    if code.startswith(("METRIC_", "LANGUAGE_", "COLLECTION_")):
        return "repair_structured_checkpoint_json"
    if code.startswith("EVIDENCE_"):
        return "repair_evidence_refs"
    if code.startswith(("ARTIFACT_", "HTML_", "XLSX_", "CSV_", "JSON_", "PDF_", "MARKDOWN_", "STATIC_SITE_")):
        return "repair_artifact_against_findings"
    if code.startswith(("STAGED_", "SPREADSHEET_")):
        return "repair_structured_checkpoint_json"
    if code.startswith("PATH_"):
        return "fix_path_within_allowed_roots"
    if code.startswith("TOOL_PROTOCOL_"):
        return "repair_tool_call"
    if code in {"TOOL_NOT_REGISTERED", "TOOL_MANIFEST_EFFECT_MISSING"} or code.startswith("TOOL_MANIFEST_"):
        return "choose_registered_tool_or_request_capability"
    if code.startswith("TOOL_"):
        return "repair_tool_arguments_or_choose_allowed_tool"
    if code.startswith(("CONTRACT_", "EFFECTIVE_CONTRACT_", "UNKNOWN_VERIFIER")):
        return "repair_effective_contract"
    if code.startswith(("STATE_", "FINAL_CLOSEOUT_", "ACCEPTANCE_")):
        return "rerun_acceptance_after_repair"
    return "repair_against_contract_findings"


def next_status(status: str) -> str:
    return {
        "repair_required": "REPAIRING",
        "needs_user_input": "WAITING_APPROVAL",
        "recovering": "RECOVERING",
        "blocked": "BLOCKED",
    }.get(status, "BLOCKED")


__all__ = [
    "action_status",
    "next_status",
    "recommended_action",
    "recovery_category",
]
