"""Recovery actions, models, classification, and envelope."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

# ===========================================================================
# recovery actions
# ===========================================================================

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


# ===========================================================================
# recovery models
# ===========================================================================

@dataclass(frozen=True)
class RecoveryEnvelopeRequest:
    gate: str
    status: str
    allowed: bool
    findings: tuple[dict[str, Any], ...] | list[dict[str, Any]]
    recommended_action: str = ""
    evidence: dict[str, Any] | None = None


@dataclass(frozen=True)
class RecoveryEnvelope:
    status: str
    can_auto_repair: bool
    requires_user: bool
    terminal: bool
    next_status: str
    recommended_action: str
    finding_codes: tuple[str, ...]
    actions: tuple[dict[str, Any], ...] = ()
    message_zh: str = ""
    evidence: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "can_auto_repair": self.can_auto_repair,
            "requires_user": self.requires_user,
            "terminal": self.terminal,
            "next_status": self.next_status,
            "recommended_action": self.recommended_action,
            "finding_codes": list(self.finding_codes),
            "actions": [dict(item) for item in self.actions],
            "message_zh": self.message_zh,
            "evidence": dict(self.evidence),
        }


# ===========================================================================
# recovery classification
# ===========================================================================

_APPROVAL_WAIT_CODES = {"APPROVAL_REQUIRED", "APPROVAL_NOT_FOUND", "APPROVAL_PENDING"}


def action_status(status: str, code: str) -> str:
    protocol_status = str(status or "").strip()
    normalized_code = _normalized_code(code)
    if hard_stop_code(normalized_code):
        return "blocked"
    if (
        normalized_code in _APPROVAL_WAIT_CODES
        or normalized_code == "TOOL_NOT_ALLOWED"
        or protocol_status in {"NEED_APPROVAL", "WAITING_HUMAN"}
    ):
        return "needs_user_input"
    if protocol_status == "RECOVERING" or recovering_code(normalized_code):
        return "recovering"
    if protocol_status in {"NEED_REPAIR", "DENY", "BLOCKED"} and repairable_code(normalized_code):
        return "repair_required"
    return "blocked"


def repairable_code(code: str) -> bool:
    code = _normalized_code(code)
    return code.startswith(
        (
            "ACCEPTANCE_", "ARTIFACT_", "BUILDER_", "COLLABORATION_",
            "COLLECTION_", "CSV_", "DELIVERY_", "DOCUMENT_", "DOCX_",
            "EVIDENCE_", "FACT_", "FINAL_CLOSEOUT_", "HTML_", "JSON_",
            "LANGUAGE_", "MARKDOWN_", "METRIC_", "PATH_", "PDF_",
            "SPREADSHEET_", "SOURCE_", "STAGED_", "STATE_TRANSITION_",
            "TASK_PROGRESS_", "TARGET_COVERAGE_", "STATIC_SITE_",
            "TOOL_PROTOCOL_", "TOOL_INVALID_", "TOOL_NOT_REGISTERED",
            "TOOL_MANIFEST_", "XLSX_",
        )
    ) or code in {
        "CLOSEOUT_ARTIFACTS_MISSING", "CLOSEOUT_REPORT_REF_MISSING",
        "CONTRACT_SCHEMA_INVALID", "EFFECTIVE_CONTRACT_ARTIFACTS_INVALID",
        "EFFECTIVE_CONTRACT_MISSING", "UNKNOWN_VERIFIER",
    }


def recovering_code(code: str) -> bool:
    code = _normalized_code(code)
    return code.startswith(("AUDIT_", "RECOVERY_", "RUNLOG_", "SCOPE_")) or code in {
        "REQUEST_ID_MISSING", "RUN_ID_MISSING", "STATE_CORRUPT",
        "TASK_ID_MISSING", "WORKSPACE_ROOT_MISSING",
        "EFFECTIVE_CONTRACT_HASH_MISSING", "EFFECTIVE_CONTRACT_REF_MISSING",
        "TOOL_MANIFEST_REF_MISSING", "REVALIDATE_ARTIFACT_BEFORE_RERUN",
    }


def hard_stop_code(code: str) -> bool:
    code = _normalized_code(code)
    return code in {
        "APPROVAL_ALREADY_USED", "APPROVAL_BINDING_MISMATCH",
        "APPROVAL_EXPIRED", "APPROVAL_REJECTED", "APPROVER_NOT_AUTHORIZED",
        "STATE_CHECKSUM_MISMATCH", "USER_CANCELLED",
    }


def recovery_category(code: str) -> str:
    code = _normalized_code(code)
    if code.startswith(("ARTIFACT_", "BUILDER_", "DOCUMENT_", "DOCX_", "HTML_",
                         "XLSX_", "CSV_", "JSON_", "PDF_", "MARKDOWN_",
                         "SPREADSHEET_", "STATIC_SITE_")):
        return "artifact"
    if code.startswith(("EVIDENCE_", "FACT_", "METRIC_", "LANGUAGE_",
                         "COLLABORATION_", "COLLECTION_", "SOURCE_",
                         "TASK_PROGRESS_", "TARGET_COVERAGE_")):
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
    status = str(status or "").strip()
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
    if code.startswith(("EVIDENCE_", "FACT_", "SOURCE_")):
        return RecoveryAction.REPAIR_EVIDENCE_REFS.value
    if code.startswith(("ARTIFACT_", "BUILDER_", "DOCUMENT_", "DOCX_", "HTML_",
                         "XLSX_", "CSV_", "JSON_", "PDF_", "MARKDOWN_", "STATIC_SITE_")):
        return RecoveryAction.REPAIR_ARTIFACT_AGAINST_FINDINGS.value
    if code.startswith(("STAGED_", "SPREADSHEET_")):
        return RecoveryAction.REPAIR_STRUCTURED_CHECKPOINT_JSON.value
    if code.startswith("PATH_"):
        return RecoveryAction.FIX_PATH_WITHIN_ALLOWED_ROOTS.value
    if code.startswith("TOOL_PROTOCOL_"):
        return RecoveryAction.REPAIR_TOOL_CALL.value
    if (code in {"TOOL_NOT_REGISTERED", "TOOL_MANIFEST_EFFECT_MISSING"}
            or code.startswith("TOOL_MANIFEST_")):
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


# ===========================================================================
# recovery envelope
# ===========================================================================

def recovery_envelope_from_gate_payload(request: RecoveryEnvelopeRequest) -> RecoveryEnvelope | None:
    if request.allowed:
        return None
    normalized = [_finding_payload(item) for item in request.findings if isinstance(item, dict)]
    if not normalized:
        normalized = [{"code": _default_code(request.status), "severity": "P1", "message": "", "evidence": {}}]
    actions = tuple(_recovery_action(request.gate, request.status, item) for item in normalized)
    envelope_status = _envelope_status(request.status, actions)
    return RecoveryEnvelope(
        status=envelope_status,
        can_auto_repair=envelope_status == "repair_required",
        requires_user=envelope_status == "needs_user_input",
        terminal=envelope_status == "blocked",
        next_status=next_status(envelope_status),
        recommended_action=request.recommended_action or _first_action(actions),
        finding_codes=tuple(dict.fromkeys(str(item["code"]) for item in actions if str(item.get("code") or ""))),
        actions=actions,
        message_zh=_envelope_message(request.gate, envelope_status, actions),
        evidence=dict(request.evidence or {}),
    )


def recovery_actions_from_gate_decisions(decisions: list[Any] | tuple[Any, ...]) -> list[dict[str, Any]]:
    actions: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    for payload in _decision_payloads(decisions):
        _append_recovery_actions(actions, seen, payload)
    return actions


def _decision_payloads(decisions: list[Any] | tuple[Any, ...]) -> list[dict[str, Any]]:
    return [payload for decision in decisions if isinstance(payload := _decision_payload(decision), dict)]


def _decision_payload(decision: Any) -> dict[str, Any] | None:
    payload = decision.to_dict() if hasattr(decision, "to_dict") else decision
    return payload if isinstance(payload, dict) else None


def _append_recovery_actions(
    actions: list[dict[str, Any]], seen: set[tuple[str, str, str]], payload: dict[str, Any],
) -> None:
    recovery = payload.get("recovery")
    raw_actions = recovery.get("actions") if isinstance(recovery, dict) else None
    for action in raw_actions if isinstance(raw_actions, list) else []:
        if isinstance(action, dict):
            _append_unique_action(actions, seen, action)


def _append_unique_action(
    actions: list[dict[str, Any]], seen: set[tuple[str, str, str]], action: dict[str, Any],
) -> None:
    identity = (
        str(action.get("source_gate") or ""),
        str(action.get("code") or ""),
        str(action.get("recommended_action") or ""),
    )
    if identity in seen:
        return
    seen.add(identity)
    actions.append(dict(action))


def _finding_payload(item: dict[str, Any]) -> dict[str, Any]:
    evidence = item.get("evidence")
    return {
        "code": str(item.get("code") or "").strip() or "CONTRACT_FINDING",
        "severity": str(item.get("severity") or "P1").strip(),
        "message": str(item.get("message") or "").strip(),
        "evidence": dict(evidence) if isinstance(evidence, dict) else {},
    }


def _recovery_action(gate: str, status: str, finding: dict[str, Any]) -> dict[str, Any]:
    original_code = str(finding.get("code") or "CONTRACT_FINDING").upper()
    evidence = dict(finding.get("evidence") or {})
    code = _primary_code(original_code, evidence)
    category = recovery_category(code)
    classified = action_status(status, code)
    action = recommended_action(code, classified)
    return {
        "code": code,
        "source_code": original_code,
        "source_gate": gate,
        "category": category,
        "retryable": classified == "repair_required",
        "can_auto_repair": classified == "repair_required",
        "requires_user": classified == "needs_user_input",
        "terminal": classified == "blocked",
        "next_status": next_status(classified),
        "recommended_action": action,
        "recovery_hint": _hint_zh(category, classified, action),
        "message_zh": _action_message_zh(gate, code, classified, action),
        "finding_codes": [code] if code == original_code else [original_code, code],
        "evidence": evidence,
    }


def _primary_code(code: str, evidence: dict[str, Any]) -> str:
    child_code = str(evidence.get("child_code") or "").strip().upper()
    if child_code and code.startswith(("FINAL_CLOSEOUT_", "ACCEPTANCE_")):
        return child_code
    return code


def _envelope_status(status: str, actions: tuple[dict[str, Any], ...]) -> str:
    statuses = {str(item.get("next_status") or "") for item in actions}
    if "WAITING_APPROVAL" in statuses or "WAITING_USER" in statuses:
        return "needs_user_input"
    if "REPAIRING" in statuses:
        return "repair_required"
    if "RECOVERING" in statuses:
        return "recovering"
    normalized = str(status or "").strip().upper()
    if normalized == "NEED_APPROVAL":
        return "needs_user_input"
    if normalized == "RECOVERING":
        return "recovering"
    if normalized == "NEED_REPAIR":
        return "repair_required"
    return "blocked"


def _first_action(actions: tuple[dict[str, Any], ...]) -> str:
    for action in actions:
        value = str(action.get("recommended_action") or "").strip()
        if value:
            return value
    return RecoveryAction.REPORT_BLOCKER.value


def _default_code(status: str) -> str:
    normalized = str(status or "").strip()
    if normalized == "NEED_REPAIR":
        return "CONTRACT_REPAIR_REQUIRED"
    if normalized == "NEED_APPROVAL":
        return "APPROVAL_REQUIRED"
    if normalized == "RECOVERING":
        return "RECOVERY_REQUIRED"
    return "CONTRACT_BLOCKED"


def _envelope_message(gate: str, status: str, actions: tuple[dict[str, Any], ...]) -> str:
    codes = ", ".join(str(item.get("code") or "") for item in actions if str(item.get("code") or ""))
    if status == "repair_required":
        return f"合同门 {gate} 未通过，可自动返工；请按结构化动作修复后重新验收。finding={codes}"
    if status == "needs_user_input":
        return f"合同门 {gate} 需要用户确认或审批；不能自动继续执行。finding={codes}"
    if status == "recovering":
        return f"合同门 {gate} 需要先恢复账本或 checkpoint；恢复完成后再继续。finding={codes}"
    return f"合同门 {gate} 已阻断；当前不能安全自动继续。finding={codes}"


def _action_message_zh(gate: str, code: str, status: str, action: str) -> str:
    if status == "repair_required":
        return f"{gate} 发现 {code}，请执行 {action} 后重新验收。"
    if status == "needs_user_input":
        return f"{gate} 发现 {code}，需要用户确认或审批后才能继续。"
    if status == "recovering":
        return f"{gate} 发现 {code}，需要先从 checkpoint 或账本恢复。"
    return f"{gate} 发现 {code}，已停止自动继续。"


def _hint_zh(category: str, status: str, action: str) -> str:
    if status == "needs_user_input":
        return "该问题不能由模型自行绕过；请通过用户输入、审批记录或权限变更解决。"
    if status == "recovering":
        return "先恢复结构化状态、runlog、tool trace 或 checkpoint，恢复成功后再重新验收。"
    if status == "blocked":
        return "当前继续执行有风险或缺少不可替代事实；请记录阻塞原因并停止自动重试。"
    if category == "evidence":
        return "补齐结构化 source_refs、claims、时间窗口、字段来源或目标语言字段，再重新验收。"
    if category == "artifact":
        return "修复或重建目标产物，并确保产物路径、格式、内容和 provenance 都满足合同。"
    if category == "tool":
        return "按工具 schema、可用工具清单和运行时门修正工具调用后再执行。"
    if category == "path":
        return "把路径修正到允许工作区或已授权 root 内，不要使用越界、符号链接逃逸或未授权路径。"
    if category == "contract":
        return "修复有效合同的结构化字段，确保合同 schema、规则和产物声明可执行。"
    return f"按 {action} 修复结构化 finding 后重新验收。"


__all__ = [
    "RECOVERY_ACTIONS",
    "RecoveryAction",
    "RecoveryEnvelope",
    "RecoveryEnvelopeRequest",
    "action_status",
    "hard_stop_code",
    "known_recovery_action_values",
    "next_status",
    "recovery_action_value",
    "recovery_actions_from_gate_decisions",
    "recovery_category",
    "recovery_envelope_from_gate_payload",
    "recommended_action",
    "repairable_code",
]
