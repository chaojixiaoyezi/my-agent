
from __future__ import annotations

from typing import Any

from .offline_contract_report import (
    OfflineContractValidation,
    dict_items,
    finding,
    text,
    validation_report,
)


def validate_channel_browser_facts(facts: dict[str, Any]) -> OfflineContractValidation:
    findings: list[dict[str, object]] = []
    _validate_duplicates(facts, findings)
    _validate_routes_and_delivery(facts, findings)
    _validate_browser_events(facts, findings)
    return validation_report(findings)


def _validate_duplicates(facts: dict[str, Any], findings: list[dict[str, object]]) -> None:
    if _has_duplicate_id(facts.get("channel_events"), "event_id"):
        findings.append(finding("CHANNEL_EVENT_DUPLICATE"))
    if _has_duplicate_id(facts.get("approval_clicks"), "approval_id"):
        findings.append(finding("APPROVAL_CLICK_DUPLICATE"))


def _validate_routes_and_delivery(facts: dict[str, Any], findings: list[dict[str, object]]) -> None:
    for route in dict_items(facts.get("routes")):
        if text(route.get("expected_target")) != text(route.get("actual_target")):
            findings.append(finding("CHANNEL_ROUTE_MISMATCH"))
            break
    for delivery in dict_items(facts.get("deliveries")):
        if delivery.get("ok") is False:
            findings.append(finding("NOTIFICATION_DELIVERY_FAILED"))
            break


def _validate_browser_events(facts: dict[str, Any], findings: list[dict[str, object]]) -> None:
    for event in dict_items(facts.get("browser_events")):
        event_type = text(event.get("type"))
        if event_type == "session" and event.get("expired") is True:
            findings.append(finding("BROWSER_SESSION_EXPIRED"))
        if event_type == "dom_lookup" and event.get("found") is False:
            findings.append(finding("BROWSER_DOM_TARGET_MISSING"))
        if event_type == "click" and event.get("verified_change") is not True:
            findings.append(finding("BROWSER_CLICK_UNVERIFIED"))
        if event_type == "evidence" and not text(event.get("screenshot_ref")):
            findings.append(finding("BROWSER_EVIDENCE_MISSING"))
        if event_type == "challenge" and text(event.get("kind")):
            findings.append(finding("BROWSER_HUMAN_CHALLENGE"))


def _has_duplicate_id(value: object, field: str) -> bool:
    seen: set[str] = set()
    for item in dict_items(value):
        item_id = text(item.get(field))
        if item_id and item_id in seen:
            return True
        seen.add(item_id)
    return False


__all__ = ["validate_channel_browser_facts"]
