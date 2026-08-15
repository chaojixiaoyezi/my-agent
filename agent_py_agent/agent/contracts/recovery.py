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
#
# 错误码是结构化协议常量（如 ARTIFACT_EMPTY），第一段命名空间即"家族"。
# 全部分类事实集中在这一张策略表里：精确码优先，其次最长家族前缀，
# 未知码 fail-closed（不可修复、不可恢复 -> blocked / REPORT_BLOCKER）。
# finding payload 里显式声明的 recommended_action / category 优先于本表推导。

@dataclass(frozen=True)
class CodePolicy:
    """单个错误码（或家族）的恢复处置事实。"""

    category: str
    disposition: str = ""  # "repairable" / "recovering" / "hard_stop" / ""
    repair_action: str = ""


_EXACT_CODE_POLICIES: dict[str, CodePolicy] = {
    # 审批等待（需要用户输入）
    "APPROVAL_REQUIRED": CodePolicy("approval", "wait_user"),
    "PERSONA_WRITE_REQUIRES_TOOL": CodePolicy(
        "tool", "repairable", "repair_tool_arguments"
    ),
    "APPROVAL_NOT_FOUND": CodePolicy("approval", "wait_user"),
    "APPROVAL_PENDING": CodePolicy("approval", "wait_user"),
    "TOOL_NOT_ALLOWED": CodePolicy("tool", "wait_user", "repair_tool_arguments"),
    "TOOL_PERMISSION_DENIED": CodePolicy(
        "permission",
        "hard_stop",
        RecoveryAction.CHANGE_STRATEGY.value,
    ),
    "SUBAGENT_RETRY_REQUIRED": CodePolicy(
        "orchestration",
        "repairable",
        RecoveryAction.DISPATCH.value,
    ),
    "AUDIT_SOURCE_WORKER_SYSTEM_MANAGED": CodePolicy(
        "orchestration",
        "repairable",
        RecoveryAction.CHANGE_STRATEGY.value,
    ),
    "AUDIT_PARENT_INACTIVE": CodePolicy(
        "orchestration",
        "hard_stop",
        RecoveryAction.STOP.value,
    ),
    "AUDIT_PARENT_STATE_UNAVAILABLE": CodePolicy(
        "state",
        "recovering",
        RecoveryAction.RETRY_AFTER_BACKOFF.value,
    ),
    "AUDIT_DELIVERY_REF_INVALID": CodePolicy(
        "validation",
        "repairable",
        RecoveryAction.REPAIR_TOOL_ARGUMENTS.value,
    ),
    "AUDIT_VERDICT_IDENTITY_MODE_INVALID": CodePolicy(
        "validation",
        "repairable",
        RecoveryAction.REPAIR_TOOL_ARGUMENTS.value,
    ),
    "AUDIT_VERDICT_SHAPE_INVALID": CodePolicy(
        "validation",
        "repairable",
        RecoveryAction.REPAIR_TOOL_ARGUMENTS.value,
    ),
    "AUDIT_VERDICT_TOKEN_COVERAGE_INVALID": CodePolicy(
        "validation",
        "repairable",
        RecoveryAction.REPAIR_TOOL_ARGUMENTS.value,
    ),
    "AUDIT_EFFECTIVE_PROMPT_HOST_MARKER_FORBIDDEN": CodePolicy(
        "validation",
        "repairable",
        RecoveryAction.REPAIR_TOOL_ARGUMENTS.value,
    ),
    "AUDIT_RUN_EPOCH_MISMATCH": CodePolicy(
        "state",
        "hard_stop",
        RecoveryAction.STOP.value,
    ),
    "AUDIT_SOURCE_WORKER_WORKSPACE_MIGRATION_PENDING": CodePolicy(
        "state",
        "recovering",
        RecoveryAction.RETRY_AFTER_BACKOFF.value,
    ),
    "SOURCE_REQUEST_INVALID": CodePolicy(
        "source",
        "repairable",
        RecoveryAction.REPAIR_TOOL_ARGUMENTS.value,
    ),
    "SOURCE_SECRET_UNAVAILABLE": CodePolicy(
        "permission",
        "wait_user",
        RecoveryAction.REQUEST_PERMISSION.value,
    ),
    # 硬停（不可自动继续）
    "APPROVAL_ALREADY_USED": CodePolicy("approval", "hard_stop"),
    "APPROVAL_BINDING_MISMATCH": CodePolicy("approval", "hard_stop"),
    "APPROVAL_EXPIRED": CodePolicy("approval", "hard_stop"),
    "APPROVAL_REJECTED": CodePolicy("approval", "hard_stop"),
    "APPROVER_NOT_AUTHORIZED": CodePolicy("approval", "hard_stop"),
    "SANDBOX_UNAVAILABLE": CodePolicy("tool", "hard_stop"),
    "OWNER_SCOPE_UNAVAILABLE": CodePolicy("permission", "hard_stop"),
    "STATE_CHECKSUM_MISMATCH": CodePolicy("state", "hard_stop", "rerun_acceptance_after_repair"),
    "USER_CANCELLED": CodePolicy("approval", "hard_stop"),
    # 恢复类（先恢复账本/checkpoint）
    "REQUEST_ID_MISSING": CodePolicy("recovery", "recovering"),
    "RUN_ID_MISSING": CodePolicy("recovery", "recovering"),
    "STATE_CORRUPT": CodePolicy("state", "recovering"),
    "TASK_ID_MISSING": CodePolicy("recovery", "recovering"),
    "WORKSPACE_ROOT_MISSING": CodePolicy("recovery", "recovering"),
    "CONVERSATION_PERSISTENCE_UNAVAILABLE": CodePolicy("state", "recovering", "retry"),
    "EFFECTIVE_CONTRACT_HASH_MISSING": CodePolicy("contract", "recovering"),
    "EFFECTIVE_CONTRACT_REF_MISSING": CodePolicy("contract", "recovering"),
    "TOOL_MANIFEST_REF_MISSING": CodePolicy("tool", "recovering"),
    "REVALIDATE_ARTIFACT_BEFORE_RERUN": CodePolicy("artifact", "recovering"),
    # 可修复的精确码
    "CLOSEOUT_ARTIFACTS_MISSING": CodePolicy("contract", "repairable", "repair_against_contract_findings"),
    "CLOSEOUT_REPORT_REF_MISSING": CodePolicy("contract", "repairable", "repair_against_contract_findings"),
    "CONTRACT_SCHEMA_INVALID": CodePolicy("contract", "repairable", "repair_effective_contract"),
    "EFFECTIVE_CONTRACT_ARTIFACTS_INVALID": CodePolicy("contract", "repairable", "repair_effective_contract"),
    "EFFECTIVE_CONTRACT_MISSING": CodePolicy("contract", "repairable", "repair_effective_contract"),
    "UNKNOWN_VERIFIER": CodePolicy("contract", "repairable", "repair_effective_contract"),
    "TOOL_NOT_REGISTERED": CodePolicy("tool", "repairable", "choose_registered_tool"),
    "TOOL_NOT_FOUND": CodePolicy("tool", "repairable", "choose_registered_tool"),
    "TOOL_NAME_REQUIRED": CodePolicy("tool", "repairable", "repair_tool_call"),
    "TOOL_MANIFEST_EFFECT_MISSING": CodePolicy("tool", "repairable", "choose_registered_tool"),
    "COMMAND_EMPTY": CodePolicy("tool", "repairable", RecoveryAction.REPAIR_TOOL_ARGUMENTS.value),
    "COMMAND_ARGV_INVALID": CodePolicy("tool", "repairable", RecoveryAction.REPAIR_TOOL_ARGUMENTS.value),
    "COMMAND_PARSE_FAILED": CodePolicy("tool", "repairable", RecoveryAction.REPAIR_TOOL_CALL.value),
    "COMMAND_DANGEROUS_PATTERN_BLOCKED": CodePolicy(
        "tool", "repairable", RecoveryAction.CHANGE_STRATEGY.value
    ),
    "COMMAND_DESTRUCTIVE_DELETE_BLOCKED": CodePolicy(
        "tool", "repairable", RecoveryAction.CHANGE_STRATEGY.value
    ),
    "COMMAND_SHELL_OPERATOR_BLOCKED": CodePolicy(
        "tool", "repairable", RecoveryAction.CHANGE_STRATEGY.value
    ),
    "COMMAND_DANGEROUS_EXECUTABLE_BLOCKED": CodePolicy(
        "tool", "repairable", RecoveryAction.CHANGE_STRATEGY.value
    ),
    "COMMAND_POLICY_BLOCKED": CodePolicy(
        "tool", "repairable", RecoveryAction.CHANGE_STRATEGY.value
    ),
    "TOOL_GUARDRAIL_REPEAT_FAILURE_HINT": CodePolicy(
        "tool", "repairable", RecoveryAction.CHANGE_STRATEGY.value
    ),
    "TOOL_GUARDRAIL_REPEAT_FAILURE_BLOCKED": CodePolicy(
        "tool", "repairable", RecoveryAction.CHANGE_STRATEGY.value
    ),
    "TOOL_GUARDRAIL_NO_PROGRESS_WARNING": CodePolicy(
        "tool", "repairable", RecoveryAction.CHANGE_STRATEGY.value
    ),
    "TOOL_GUARDRAIL_NO_PROGRESS_BLOCKED": CodePolicy(
        "tool", "repairable", RecoveryAction.CHANGE_STRATEGY.value
    ),
    "WAIT_ACTIONABLE_INPUT_PENDING": CodePolicy(
        "orchestration", "repairable", RecoveryAction.CHANGE_STRATEGY.value
    ),
    "TOOL_RATE_LIMIT_IDENTITY_MISSING": CodePolicy(
        "tool", "repairable", RecoveryAction.REPAIR_TOOL_CALL_IDENTITY.value
    ),
    "TOOL_CIRCUIT_OPEN": CodePolicy(
        "tool", "repairable", RecoveryAction.RETRY_AFTER_BACKOFF.value
    ),
    "TOOL_RATE_LIMIT_EXCEEDED": CodePolicy(
        "tool", "repairable", RecoveryAction.RETRY_AFTER_BACKOFF.value
    ),
    "TOOL_OPERATION_STORE_UNAVAILABLE": CodePolicy(
        "tool", "repairable", RecoveryAction.RETRY_AFTER_BACKOFF.value
    ),
    "TOOL_OPERATION_IDENTITY_CONFLICT": CodePolicy(
        "tool", "repairable", RecoveryAction.REPAIR_TOOL_CALL_IDENTITY.value
    ),
    "TOOL_OPERATION_OUTCOME_UNKNOWN": CodePolicy(
        "tool", "hard_stop", RecoveryAction.MANUAL_REVIEW.value
    ),
    "TOOL_OPERATION_IN_FLIGHT": CodePolicy(
        "tool", "repairable", RecoveryAction.WAIT_FOR_EXISTING_OPERATION.value
    ),
    "GATE_PIPELINE_DEPENDENCY_UNSATISFIED": CodePolicy(
        "contract", "repairable", RecoveryAction.REPAIR_GATE_PIPELINE_ORDER.value
    ),
    "GATE_PIPELINE_SPEC_MISSING": CodePolicy(
        "contract", "repairable", RecoveryAction.REGISTER_GATE_PIPELINE.value
    ),
    "GATE_PIPELINE_REQUIRED_GATE_MISSING": CodePolicy(
        "contract", "repairable", RecoveryAction.REGISTER_REQUIRED_GATE.value
    ),
    "GATE_PIPELINE_EMPTY": CodePolicy(
        "contract", "repairable", RecoveryAction.REGISTER_GATE_PIPELINE.value
    ),
    "RUNTIME_GATE_DENIED": CodePolicy(
        "tool", "repairable", RecoveryAction.CHANGE_STRATEGY.value
    ),
    "CONVERSATION_TASK_ALREADY_RUNNING": CodePolicy(
        "orchestration", "repairable", "change_strategy"
    ),
    "CONVERSATION_TASK_STATE_UNAVAILABLE": CodePolicy(
        "state", "recovering", "retry"
    ),
    "CONVERSATION_TASK_BINDING_FAILED": CodePolicy(
        "orchestration", "recovering", "retry"
    ),
    "CONVERSATION_CONTEXT_REQUIRED": CodePolicy(
        "orchestration", "hard_stop"
    ),
    "NAMED_WORK_NOT_FOUND": CodePolicy(
        "orchestration", "repairable", "repair_tool_arguments"
    ),
    "NAMED_WORK_CONFLICT": CodePolicy(
        "orchestration", "repairable", "repair_tool_arguments"
    ),
    "GOAL_CONTEXT_REQUIRED": CodePolicy("orchestration", "hard_stop"),
    "GOAL_NOT_FOUND": CodePolicy(
        "orchestration", "repairable", "change_strategy"
    ),
    "GOAL_STATE_CONFLICT": CodePolicy(
        "orchestration", "repairable", "change_strategy"
    ),
    "GOAL_INVALID_REQUEST": CodePolicy(
        "tool", "repairable", "repair_tool_arguments"
    ),
    # 修复动作特例
    "TARGET_COVERAGE_MISSING": CodePolicy("evidence", "repairable", "continue"),
    "TASK_PROGRESS_OPEN_ITEMS": CodePolicy("evidence", "repairable", "continue"),
}

# 家族前缀策略：按最长前缀优先匹配（前缀即错误码协议的结构化命名空间）。
_FAMILY_POLICIES: tuple[tuple[str, CodePolicy], ...] = tuple(
    sorted(
        [
            # artifact 家族
            ("ARTIFACT_", CodePolicy("artifact", "repairable", "repair_artifact_against_findings")),
            ("BUILDER_", CodePolicy("artifact", "repairable", "repair_artifact_against_findings")),
            ("DOCUMENT_", CodePolicy("artifact", "repairable", "repair_artifact_against_findings")),
            ("DOCX_", CodePolicy("artifact", "repairable", "repair_artifact_against_findings")),
            ("HTML_", CodePolicy("artifact", "repairable", "repair_artifact_against_findings")),
            ("XLSX_", CodePolicy("artifact", "repairable", "repair_artifact_against_findings")),
            ("CSV_", CodePolicy("artifact", "repairable", "repair_artifact_against_findings")),
            ("JSON_", CodePolicy("artifact", "repairable", "repair_artifact_against_findings")),
            ("PDF_", CodePolicy("artifact", "repairable", "repair_artifact_against_findings")),
            ("MARKDOWN_", CodePolicy("artifact", "repairable", "repair_artifact_against_findings")),
            ("STATIC_SITE_", CodePolicy("artifact", "repairable", "repair_artifact_against_findings")),
            ("SPREADSHEET_", CodePolicy("artifact", "repairable", "repair_structured_checkpoint_json")),
            ("STAGED_", CodePolicy("contract", "repairable", "repair_structured_checkpoint_json")),
            # evidence 家族
            ("EVIDENCE_", CodePolicy("evidence", "repairable", "repair_evidence_refs")),
            ("FACT_", CodePolicy("evidence", "repairable", "repair_evidence_refs")),
            ("SOURCE_", CodePolicy("evidence", "repairable", "repair_evidence_refs")),
            ("TASK_PROGRESS_", CodePolicy("evidence", "repairable", "repair_evidence_refs")),
            ("TARGET_COVERAGE_", CodePolicy("evidence", "repairable")),
            ("METRIC_", CodePolicy("evidence", "repairable", "repair_structured_checkpoint_json")),
            ("LANGUAGE_", CodePolicy("evidence", "repairable", "repair_structured_checkpoint_json")),
            ("COLLECTION_", CodePolicy("evidence", "repairable", "repair_structured_checkpoint_json")),
            ("COLLABORATION_", CodePolicy("evidence", "repairable", "continue_collaboration")),
            # tool 家族
            ("TOOL_PROTOCOL_", CodePolicy("tool", "repairable", "repair_tool_call")),
            ("TOOL_MANIFEST_", CodePolicy("tool", "repairable", "choose_registered_tool")),
            ("TOOL_INVALID_", CodePolicy("tool", "repairable", "repair_tool_arguments")),
            # 模型自己可改正的调用形错误（缺参/类型错/参数被拦），修参数后重试
            ("TOOL_PARAMETER_", CodePolicy("tool", "repairable", "repair_tool_arguments")),
            ("TOOL_", CodePolicy("tool", "", "repair_tool_arguments")),
            # path / approval
            ("PATH_", CodePolicy("path", "repairable", "fix_path_within_allowed_roots")),
            ("APPROVAL_", CodePolicy("approval", "")),
            ("USER_", CodePolicy("approval", "")),
            # recovery 家族
            ("AUDIT_", CodePolicy("recovery", "recovering")),
            ("RECOVERY_", CodePolicy("recovery", "recovering")),
            ("RUNLOG_", CodePolicy("recovery", "recovering")),
            ("SCOPE_", CodePolicy("contract", "recovering")),
            # state / closeout / acceptance 家族
            ("STATE_TRANSITION_", CodePolicy("state", "repairable", "rerun_acceptance_after_repair")),
            ("STATE_", CodePolicy("state", "", "rerun_acceptance_after_repair")),
            ("FINAL_CLOSEOUT_", CodePolicy("state", "repairable", "rerun_acceptance_after_repair")),
            ("ACCEPTANCE_", CodePolicy("state", "repairable", "rerun_acceptance_after_repair")),
            # delivery / contract 家族
            ("DELIVERY_", CodePolicy("contract", "repairable", "repair_against_contract_findings")),
            ("CONTRACT_", CodePolicy("contract", "", "repair_effective_contract")),
            ("EFFECTIVE_CONTRACT_", CodePolicy("contract", "", "repair_effective_contract")),
        ],
        key=lambda item: len(item[0]),
        reverse=True,
    )
)

_FAIL_CLOSED_POLICY = CodePolicy("contract", "")


def code_policy(code: str) -> CodePolicy:
    """查询错误码的恢复处置事实；未知码 fail-closed。"""
    normalized = _normalized_code(code)
    exact = _EXACT_CODE_POLICIES.get(normalized)
    if exact is not None:
        return exact
    for prefix, policy in _FAMILY_POLICIES:
        if normalized.startswith(prefix):
            return policy
    return _FAIL_CLOSED_POLICY


def action_status(status: str, code: str) -> str:
    protocol_status = str(status or "").strip()
    policy = code_policy(code)
    if policy.disposition == "hard_stop":
        return "blocked"
    if policy.disposition == "wait_user" or protocol_status in {"NEED_APPROVAL", "WAITING_HUMAN"}:
        return "needs_user_input"
    if protocol_status == "RECOVERING" or policy.disposition == "recovering":
        return "recovering"
    if protocol_status in {"NEED_REPAIR", "DENY", "BLOCKED"} and policy.disposition == "repairable":
        return "repair_required"
    return "blocked"


def repairable_code(code: str) -> bool:
    return code_policy(code).disposition == "repairable"


def recovering_code(code: str) -> bool:
    return code_policy(code).disposition == "recovering"


def hard_stop_code(code: str) -> bool:
    return code_policy(code).disposition == "hard_stop"


def recovery_category(code: str) -> str:
    return code_policy(code).category


def recommended_action(code: str, status: str) -> str:
    status = str(status or "").strip()
    if status == "needs_user_input":
        return RecoveryAction.REQUEST_USER_INPUT.value
    if status == "recovering":
        return RecoveryAction.RECOVER_FROM_CHECKPOINT.value
    if status == "blocked":
        return RecoveryAction.REPORT_BLOCKER.value
    policy = code_policy(code)
    if policy.repair_action:
        return policy.repair_action
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
    payload = {
        "code": str(item.get("code") or "").strip() or "CONTRACT_FINDING",
        "severity": str(item.get("severity") or "P1").strip(),
        "message": str(item.get("message") or "").strip(),
        "evidence": dict(evidence) if isinstance(evidence, dict) else {},
    }
    # 开放世界：finding 可以显式声明结构化恢复字段，声明优先于注册表推导。
    declared_action = str(item.get("recommended_action") or "").strip()
    if declared_action:
        payload["recommended_action"] = declared_action
    declared_category = str(item.get("category") or "").strip()
    if declared_category:
        payload["category"] = declared_category
    return payload


def _recovery_action(gate: str, status: str, finding: dict[str, Any]) -> dict[str, Any]:
    original_code = str(finding.get("code") or "CONTRACT_FINDING").upper()
    evidence = dict(finding.get("evidence") or {})
    code = _primary_code(original_code, evidence)
    category = str(finding.get("category") or "").strip() or recovery_category(code)
    classified = action_status(status, code)
    action = _declared_action(finding) or recommended_action(code, classified)
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


def _declared_action(finding: dict[str, Any]) -> str:
    """finding 显式声明的恢复动作；非当前协议枚举值一律忽略（不做别名兼容）。"""
    declared = str(finding.get("recommended_action") or "").strip()
    if declared and declared in known_recovery_action_values():
        return declared
    return ""


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
    if statuses and statuses <= {"BLOCKED"}:
        # 所有 finding 动作都已判定 blocked 时，信封不允许再被门状态兜成可修复。
        return "blocked"
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
