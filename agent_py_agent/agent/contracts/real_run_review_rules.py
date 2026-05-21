# LLM: Real run review rules map stable machine error codes to generic failure tags and priorities.
# 模块用途: 集中维护真实运行复盘的通用错误码前缀、阶段和 P0/P1 优先级规则。

from __future__ import annotations

TAG_RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("artifact_missing", ("ARTIFACT_MISSING", "STAGED_ARTIFACT_MISSING")),
    ("artifact_empty", ("ARTIFACT_EMPTY",)),
    (
        "artifact_path_mismatch",
        ("ARTIFACT_PATH_MISMATCH", "ARTIFACT_OUT_OF_BOUNDS", "ARTIFACT_PATH_OUTSIDE_WORKSPACE"),
    ),
    ("staged_checkpoint_empty", ("STAGED_JSON_NO_ROWS",)),
    (
        "structured_columns_missing",
        ("XLSX_MISSING_REQUIRED_COLUMNS", "STAGED_JSON_REQUIRED_COLUMNS_MISSING"),
    ),
    (
        "evidence_claims_missing",
        ("EVIDENCE_REQUIRED_FIELD_MISSING", "EVIDENCE_SOURCE_MISSING", "EVIDENCE_CLAIM_UNSOURCED"),
    ),
    ("static_site_contract_failed", ("STATIC_SITE_",)),
    ("tool_failed", ("TOOL_FAILED", "TOOL_RESULT_FAILED", "TOOL_TRACE_ERROR_CODE_MISSING")),
    ("tool_schema_failed", ("TOOL_SCHEMA", "TOOL_ARGUMENT", "UNKNOWN_TOOL")),
    ("tool_result_format_failed", ("TOOL_RESULT_VALIDATION", "TOOL_RETURN_FORMAT")),
    ("repeated_tool_blocked", ("REPEATED_TOOL", "NO_PROGRESS_REPEATED_TOOL")),
    ("dry_run_real_conflict", ("DRY_RUN_CLAIMED_REAL", "REAL_RUN_IN_DRY_RUN")),
    ("approval_anomaly", ("APPROVAL_",)),
    ("invalid_state_transition", ("STATE_TRANSITION_INVALID",)),
    ("context_contract_lost", ("CONTEXT_BUNDLE", "COMPACT_REF_MISSING", "CONTRACT_HASH_MISMATCH")),
    ("open_write_session_blocked", ("OPEN_FILE_WRITE_SESSION_BLOCKED",)),
    ("delivery_repair_blocked", ("DELIVERY_REQUIRED_REPAIR_BLOCKED",)),
    ("local_progress_blocked", ("LOCAL_PROGRESS_GUARD_BLOCKED",)),
    ("bootstrap_materialization_blocked", ("BOOTSTRAP_MATERIALIZATION_BLOCKED",)),
)

STAGE_BY_TAG = {
    "artifact_missing": "artifact",
    "artifact_empty": "artifact",
    "artifact_path_mismatch": "artifact",
    "staged_checkpoint_empty": "artifact",
    "structured_columns_missing": "acceptance",
    "evidence_claims_missing": "acceptance",
    "static_site_contract_failed": "acceptance",
    "tool_failed": "tool",
    "tool_schema_failed": "tool",
    "tool_result_format_failed": "tool",
    "repeated_tool_blocked": "loop",
    "dry_run_real_conflict": "approval",
    "approval_anomaly": "approval",
    "invalid_state_transition": "state",
    "context_contract_lost": "context",
    "open_write_session_blocked": "artifact",
    "delivery_repair_blocked": "recovery",
    "local_progress_blocked": "loop",
    "bootstrap_materialization_blocked": "recovery",
}

P0_TAGS = {"dry_run_real_conflict", "invalid_state_transition"}
P1_TAGS = {
    "artifact_missing",
    "artifact_empty",
    "artifact_path_mismatch",
    "staged_checkpoint_empty",
    "structured_columns_missing",
    "evidence_claims_missing",
    "static_site_contract_failed",
    "tool_failed",
    "tool_schema_failed",
    "tool_result_format_failed",
    "repeated_tool_blocked",
    "approval_anomaly",
    "context_contract_lost",
    "open_write_session_blocked",
    "delivery_repair_blocked",
    "local_progress_blocked",
    "bootstrap_materialization_blocked",
}

__all__ = ["P0_TAGS", "P1_TAGS", "STAGE_BY_TAG", "TAG_RULES"]
