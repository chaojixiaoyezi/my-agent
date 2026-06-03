from __future__ import annotations


def test_channel_rejects_duplicate_events_and_approval_clicks() -> None:
    from agent_py_agent.agent.contracts.offline_channel_browser_contract import (
        validate_channel_browser_facts,
    )

    result = validate_channel_browser_facts(
        {
            "channel_events": [{"event_id": "evt-1"}, {"event_id": "evt-1"}],
            "approval_clicks": [{"approval_id": "ap-1"}, {"approval_id": "ap-1"}],
        }
    )

    assert result.error_codes == ("CHANNEL_EVENT_DUPLICATE", "APPROVAL_CLICK_DUPLICATE")


def test_channel_rejects_route_mismatch_and_delivery_failure() -> None:
    from agent_py_agent.agent.contracts.offline_channel_browser_contract import (
        validate_channel_browser_facts,
    )

    result = validate_channel_browser_facts(
        {
            "routes": [{"expected_target": "session:a", "actual_target": "session:b"}],
            "deliveries": [{"delivery_id": "d1", "ok": False}],
        }
    )

    assert result.error_codes == ("CHANNEL_ROUTE_MISMATCH", "NOTIFICATION_DELIVERY_FAILED")


def test_browser_contract_rejects_unverified_actions_and_human_challenges() -> None:
    from agent_py_agent.agent.contracts.offline_channel_browser_contract import (
        validate_channel_browser_facts,
    )

    result = validate_channel_browser_facts(
        {
            "browser_events": [
                {"type": "session", "expired": True},
                {"type": "dom_lookup", "found": False},
                {"type": "click", "verified_change": False},
                {"type": "evidence", "screenshot_ref": ""},
                {"type": "challenge", "kind": "mfa"},
            ]
        }
    )

    assert result.error_codes == (
        "BROWSER_SESSION_EXPIRED",
        "BROWSER_DOM_TARGET_MISSING",
        "BROWSER_CLICK_UNVERIFIED",
        "BROWSER_EVIDENCE_MISSING",
        "BROWSER_HUMAN_CHALLENGE",
    )
