"""W0 错误码注册表合同单测：新增原因码完整注册且已退役旧码已移除。"""

from __future__ import annotations

import pytest

from agent_py_agent.agent.contracts.error_taxonomy import ERROR_CONTRACTS, error_contract
from agent_py_agent.agent.contracts.recovery import RECOVERY_ACTIONS, RecoveryAction

EXPECTED_CONTRACTS: tuple[tuple[str, str, bool, str], ...] = (
    ("INDEX_TAIL_INCOMPLETE", "storage", True, RecoveryAction.RETRY_AFTER_BACKOFF.value),
    ("INDEX_ROW_EXCEEDS_SLICE", "storage", False, RecoveryAction.REPORT_BLOCKER.value),
    ("INPUT_RECEIPT_SCHEMA_UNSUPPORTED", "data_corruption", False, RecoveryAction.REPORT_BLOCKER.value),
    ("INPUT_RECEIPT_STATE_INVALID", "data_corruption", False, RecoveryAction.REPORT_BLOCKER.value),
    ("INPUT_RECEIPT_REQUEST_ID_MISSING", "data_corruption", False, RecoveryAction.REPORT_BLOCKER.value),
    ("INPUT_RECEIPT_CLIENT_DIGEST_MISSING", "data_corruption", False, RecoveryAction.REPORT_BLOCKER.value),
    ("INPUT_RECEIPT_TIMESTAMP_INVALID", "data_corruption", False, RecoveryAction.REPORT_BLOCKER.value),
    ("INPUT_RECEIPT_TRANSPORT_REQUIRES_V3", "data_corruption", False, RecoveryAction.REPORT_BLOCKER.value),
    ("INPUT_RECEIPT_LEGACY_SCHEMA", "data_corruption", False, RecoveryAction.REPORT_BLOCKER.value),
    ("INPUT_RECEIPT_EMPTY", "data_corruption", False, RecoveryAction.REPORT_BLOCKER.value),
    ("INPUT_RECEIPT_UNREADABLE", "data_corruption", False, RecoveryAction.REPORT_BLOCKER.value),
    ("INPUT_RECEIPT_OWNER_BINDING_INVALID", "data_corruption", False, RecoveryAction.REPORT_BLOCKER.value),
    ("INPUT_RECEIPT_OWNER_BINDING_MISMATCH", "data_corruption", False, RecoveryAction.REPORT_BLOCKER.value),
    ("INPUT_RECEIPT_PREPARED_ID_MISMATCH", "data_corruption", False, RecoveryAction.REPORT_BLOCKER.value),
    ("INPUT_RECEIPT_CLIENT_DIGEST_MISMATCH", "data_corruption", False, RecoveryAction.REPORT_BLOCKER.value),
    ("INPUT_RECEIPT_PAYLOAD_DIGEST_MISMATCH", "data_corruption", False, RecoveryAction.REPORT_BLOCKER.value),
    ("INPUT_RECEIPT_UNCLASSIFIED", "data_corruption", False, RecoveryAction.REPORT_BLOCKER.value),
    ("METADATA_ONLY_SUPPRESSES_LOG_PAGES", "input", True, RecoveryAction.REPAIR_TOOL_ARGUMENTS.value),
    ("SCHEDULER_POSTCHECK_FAILED", "business", True, RecoveryAction.REPORT_BLOCKER.value),
    ("SCHEDULER_OUTPUT_STALLED", "business", True, RecoveryAction.REPORT_BLOCKER.value),
    ("SCHEDULER_POSTCHECK_UNAVAILABLE", "storage", True, RecoveryAction.RETRY_AFTER_BACKOFF.value),
)


@pytest.mark.parametrize("code,category,retryable,action", EXPECTED_CONTRACTS)
def test_pp1118_new_error_codes_registered_with_expected_contract(
    code: str,
    category: str,
    retryable: bool,
    action: str,
) -> None:
    contract = error_contract(code)
    assert code in ERROR_CONTRACTS
    assert contract.code == code
    assert contract.category == category
    assert contract.retryable is retryable
    assert contract.recommended_action == action
    assert contract.recommended_action in RECOVERY_ACTIONS
    assert contract.recovery_hint.strip()
    if code.startswith("INPUT_RECEIPT_"):
        assert "audit_records message_lifecycle" in contract.recovery_hint
        assert "禁止原样重放" in contract.recovery_hint


def test_pp1118_retired_index_error_code_removed() -> None:
    retired = "INDEX_ROW_EXCEEDS_SLICE_OR_INCOMPLETE"
    assert retired not in ERROR_CONTRACTS
    assert error_contract(retired).code == "UNKNOWN_ERROR"
