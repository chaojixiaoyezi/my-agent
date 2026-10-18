"""原后台/设施错误保持可行动分类，不被兜底解释为程序错误或可直接重放。"""
import pytest
from agent.contracts.error_taxonomy import error_contract


@pytest.mark.parametrize(
    ("code", "category"),
    [("GATEWAY_PREDECESSOR_TIMEOUT", "state"),
     ("COORDINATOR_PLATFORM_OR_MODE_UNSUPPORTED", "contract"),
     ("GLOBAL_WRITE_SERVICE_UNAVAILABLE", "state"),
     ("COORDINATOR_BUSINESS_NOT_READY", "state"),
     ("MEMORY_RETENTION_POLICY_CHANGED", "state"),
     ("GATEWAY_MULTIPROCESS_RESTART_UNSUPPORTED", "state")],
)
def test_frontier_errors_never_offer_blind_reexecution(code, category):
    result = error_contract(code)
    assert result.code == code and result.category == category
    assert result.retryable is False
    assert result.recommended_action == "report_blocker"
    assert result.recovery_hint


def test_approval_retirement_is_not_a_retry_or_completed_cancellation():
    result = error_contract("APPROVAL_RETIREMENT_UNCONFIRMED")
    assert result.code == "APPROVAL_RETIREMENT_UNCONFIRMED"
    assert result.category == "state"
    assert result.retryable is False
    assert result.recommended_action == "manual_review"
    assert result.recovery_hint
