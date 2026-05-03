from __future__ import annotations

"""Tests for agent_py_agent.agent.log_analysis.cases.case_store."""

from pathlib import Path

import pytest

from agent_py_agent.agent.log_analysis.cases.case_store import (
    CaseStore,
    dedup_key_for_finding,
    min_priority,
    priority_for_score,
)
from agent_py_agent.agent.log_analysis.models import (
    CaseRecord,
    EvidenceRef,
    Finding,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_finding(
    finding_id: str = "f-001",
    detector_id: str = "bruteforce_then_success",
    risk_score: float = 0.9,
    entities: dict | None = None,
    window: list[str] | None = None,
    confidence: float | None = None,
    hypothesis: str = "",
    evidence_refs: list | None = None,
    gaps: list[str] | None = None,
    next_queries: list | None = None,
) -> Finding:
    return Finding(
        finding_id=finding_id,
        detector_id=detector_id,
        risk_score=risk_score,
        entities=entities if entities is not None else {"attacker_ip": ["1.2.3.4"], "victim_ip": ["10.0.0.1"]},
        window=window if window is not None else ["2025-01-01T00:00:00Z", "2025-01-01T01:00:00Z"],
        confidence=confidence,
        hypothesis=hypothesis,
        evidence_refs=evidence_refs if evidence_refs is not None else [EvidenceRef(evidence_id="ev-1")],
        gaps=gaps if gaps is not None else [],
        next_queries=next_queries if next_queries is not None else [],
    )


# ---------------------------------------------------------------------------
# priority_for_score
# ---------------------------------------------------------------------------

class TestPriorityForScore:
    def test_p0_high_score(self) -> None:
        assert priority_for_score(0.95) == "P0"

    def test_p1_score(self) -> None:
        assert priority_for_score(0.75) == "P1"

    def test_p2_score(self) -> None:
        assert priority_for_score(0.6) == "P2"

    def test_p3_low_score(self) -> None:
        assert priority_for_score(0.3) == "P3"

    def test_boundary_085(self) -> None:
        assert priority_for_score(0.85) == "P0"

    def test_boundary_070(self) -> None:
        assert priority_for_score(0.7) == "P1"

    def test_boundary_055(self) -> None:
        assert priority_for_score(0.55) == "P2"

    def test_clamp_above_1(self) -> None:
        assert priority_for_score(1.5) == "P0"

    def test_clamp_below_0(self) -> None:
        assert priority_for_score(-0.5) == "P3"

    def test_non_numeric_returns_p3(self) -> None:
        assert priority_for_score("bad") == "P3"

    def test_zero_score(self) -> None:
        assert priority_for_score(0.0) == "P3"


# ---------------------------------------------------------------------------
# min_priority
# ---------------------------------------------------------------------------

class TestMinPriority:
    def test_p0_beats_p1(self) -> None:
        assert min_priority("P0", "P1") == "P0"

    def test_p3_beaten_by_p2(self) -> None:
        assert min_priority("P3", "P2") == "P2"

    def test_same_priority(self) -> None:
        assert min_priority("P1", "P1") == "P1"

    def test_unknown_defaults_high(self) -> None:
        # Unknown priority values get order=99, so known ones win
        assert min_priority("P0", "PX") == "P0"


# ---------------------------------------------------------------------------
# dedup_key_for_finding
# ---------------------------------------------------------------------------

class TestDedupKeyForFinding:
    def test_basic_dedup_key(self) -> None:
        finding = _make_finding(detector_id="waf_attack_success_candidate")
        key = dedup_key_for_finding(finding, window_minutes=15)
        assert "attack=1.2.3.4" in key
        assert "victim=10.0.0.1" in key
        assert "bucket=" in key

    def test_vpn_detector_includes_account(self) -> None:
        finding = _make_finding(
            detector_id="vpn_new_geo_login",
            entities={"attacker_ip": ["5.6.7.8"], "victim_ip": ["10.0.0.2"], "user": ["alice"]},
        )
        key = dedup_key_for_finding(finding, window_minutes=15)
        assert "account=alice" in key
        assert "attack=5.6.7.8" in key

    def test_bruteforce_detector_includes_account(self) -> None:
        finding = _make_finding(
            detector_id="bruteforce_then_success",
            entities={"attacker_ip": ["5.6.7.8"], "victim_ip": ["10.0.0.2"], "account": ["bob"]},
        )
        key = dedup_key_for_finding(finding, window_minutes=15)
        assert "account=bob" in key

    def test_no_entities_uses_unknown(self) -> None:
        finding = Finding(
            finding_id="f-empty",
            detector_id="generic_rule",
            risk_score=0.5,
            entities={},
            window=["2025-01-01T00:00:00Z"],
        )
        key = dedup_key_for_finding(finding, window_minutes=15)
        assert "attack=unknown" in key
        assert "victim=unknown" in key

    def test_empty_window_gives_unknown_time(self) -> None:
        finding = Finding(
            finding_id="f-no-window",
            detector_id="generic_rule",
            risk_score=0.5,
            entities={"attacker_ip": ["1.2.3.4"]},
            window=[],
        )
        key = dedup_key_for_finding(finding, window_minutes=15)
        assert "bucket=unknown-time" in key

    def test_mapping_input(self) -> None:
        data = {
            "finding_id": "f-001",
            "detector_id": "generic",
            "risk_score": 0.5,
            "entities": {"attacker_ip": ["1.1.1.1"]},
            "window": ["2025-01-01T00:10:00Z"],
        }
        key = dedup_key_for_finding(data, window_minutes=15)
        assert "attack=1.1.1.1" in key


# ---------------------------------------------------------------------------
# CaseStore
# ---------------------------------------------------------------------------

class TestCaseStore:
    def test_init_with_path(self, tmp_path: Path) -> None:
        store = CaseStore(tmp_path / "data")
        assert store.root.exists()

    def test_init_with_existing_local_store(self, tmp_path: Path) -> None:
        from agent_py_agent.agent.log_analysis.storage.local_store import LocalLogStore
        ls = LocalLogStore(tmp_path / "data")
        store = CaseStore(ls)
        assert store.root == ls.root

    def test_record_finding_creates_case(self, tmp_path: Path) -> None:
        store = CaseStore(tmp_path, min_case_confidence=0.5)
        finding = _make_finding(risk_score=0.9)
        case = store.record_finding(finding)
        assert case is not None
        assert case.risk_score == 0.9

    def test_record_finding_low_confidence_returns_none(self, tmp_path: Path) -> None:
        store = CaseStore(tmp_path, min_case_confidence=0.8)
        finding = _make_finding(risk_score=0.5, confidence=0.3)
        case = store.record_finding(finding)
        assert case is None

    def test_record_finding_uses_confidence_over_risk_score(self, tmp_path: Path) -> None:
        store = CaseStore(tmp_path, min_case_confidence=0.5)
        finding = _make_finding(risk_score=0.9, confidence=0.4)
        case = store.record_finding(finding)
        assert case is None  # confidence 0.4 < 0.5 threshold

    def test_record_finding_mapping_input(self, tmp_path: Path) -> None:
        store = CaseStore(tmp_path, min_case_confidence=0.5)
        data = _make_finding(risk_score=0.9).to_dict()
        case = store.record_finding(data)
        assert case is not None

    def test_record_findings_deduplicates_cases(self, tmp_path: Path) -> None:
        store = CaseStore(tmp_path, min_case_confidence=0.5)
        f1 = _make_finding(finding_id="f-1", risk_score=0.9)
        f2 = _make_finding(finding_id="f-2", risk_score=0.95)
        cases = store.record_findings([f1, f2])
        # Same dedup key => merged into one case
        assert len(cases) == 1

    def test_record_findings_empty(self, tmp_path: Path) -> None:
        store = CaseStore(tmp_path)
        assert store.record_findings([]) == []

    def test_list_cases_sorted_by_priority(self, tmp_path: Path) -> None:
        store = CaseStore(tmp_path, min_case_confidence=0.3)
        store.record_finding(_make_finding(finding_id="f-1", risk_score=0.5))
        store.record_finding(_make_finding(finding_id="f-2", risk_score=0.95, entities={"attacker_ip": ["9.9.9.9"], "victim_ip": ["10.0.0.99"]}))
        cases = store.list_cases()
        assert len(cases) >= 2
        # P0 should come before P2
        assert cases[0].priority <= cases[1].priority

    def test_get_case(self, tmp_path: Path) -> None:
        store = CaseStore(tmp_path, min_case_confidence=0.5)
        finding = _make_finding(risk_score=0.9)
        created = store.record_finding(finding)
        assert created is not None
        retrieved = store.get_case(created.case_id)
        assert retrieved is not None
        assert retrieved.case_id == created.case_id

    def test_get_case_not_found(self, tmp_path: Path) -> None:
        store = CaseStore(tmp_path)
        assert store.get_case("nonexistent") is None

    def test_save_case(self, tmp_path: Path) -> None:
        store = CaseStore(tmp_path)
        case = CaseRecord(case_id="c-1", title="test case", risk_score=0.5)
        store.save_case(case)
        retrieved = store.get_case("c-1")
        assert retrieved is not None
        assert retrieved.title == "test case"

    def test_save_case_mapping_input(self, tmp_path: Path) -> None:
        store = CaseStore(tmp_path)
        data = {"case_id": "c-2", "title": "mapping case", "risk_score": 0.6}
        store.save_case(data)
        retrieved = store.get_case("c-2")
        assert retrieved is not None

    def test_load_findings(self, tmp_path: Path) -> None:
        store = CaseStore(tmp_path, min_case_confidence=0.5)
        store.record_finding(_make_finding(finding_id="f-1"))
        findings = store.load_findings()
        assert len(findings) >= 1

    def test_get_case_by_dedup_key(self, tmp_path: Path) -> None:
        store = CaseStore(tmp_path, min_case_confidence=0.5)
        finding = _make_finding(risk_score=0.9)
        case = store.record_finding(finding)
        assert case is not None
        found = store.get_case_by_dedup_key(case.dedup_key)
        assert found is not None
        assert found.case_id == case.case_id

    def test_get_case_by_dedup_key_not_found(self, tmp_path: Path) -> None:
        store = CaseStore(tmp_path)
        assert store.get_case_by_dedup_key("nonexistent") is None

    def test_merge_finding_updates_risk_score(self, tmp_path: Path) -> None:
        store = CaseStore(tmp_path, min_case_confidence=0.5)
        f1 = _make_finding(finding_id="f-1", risk_score=0.7)
        f2 = _make_finding(finding_id="f-2", risk_score=0.95)
        case = store.record_finding(f1)
        assert case is not None
        case = store.record_finding(f2)
        assert case is not None
        assert case.risk_score == 0.95  # max of both

    def test_merge_finding_adds_hypothesis(self, tmp_path: Path) -> None:
        store = CaseStore(tmp_path, min_case_confidence=0.5)
        f1 = _make_finding(finding_id="f-1", hypothesis="possible breach")
        case = store.record_finding(f1)
        assert case is not None
        assert "possible breach" in case.inferences

    def test_search_store_integration(self, tmp_path: Path) -> None:
        class FakeSearchStore:
            def __init__(self) -> None:
                self.records: list[dict] = []

            def upsert_record(self, **kwargs: object) -> None:
                self.records.append(kwargs)

        ss = FakeSearchStore()
        store = CaseStore(tmp_path, min_case_confidence=0.5, search_store=ss)
        case = store.record_finding(_make_finding(risk_score=0.9))
        assert case is not None
        assert len(ss.records) == 1
        assert ss.records[0]["source_type"] == "log_analysis_case"


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

class TestCaseTypeForFindings:
    def test_suspected_zero_day(self) -> None:
        from agent_py_agent.agent.log_analysis.cases.case_store import _case_type_for_findings
        findings = [
            Finding(finding_id="f1", detector_id="waf_attack_success_candidate"),
            Finding(finding_id="f2", detector_id="web_to_process_anomaly"),
            Finding(finding_id="f3", detector_id="rare_egress_after_alert"),
        ]
        assert _case_type_for_findings(findings) == "suspected_zero_day_intrusion"

    def test_credential_intrusion(self) -> None:
        from agent_py_agent.agent.log_analysis.cases.case_store import _case_type_for_findings
        findings = [Finding(finding_id="f1", detector_id="vpn_new_geo_login")]
        assert _case_type_for_findings(findings) == "suspected_credential_intrusion"

    def test_web_intrusion_candidate(self) -> None:
        from agent_py_agent.agent.log_analysis.cases.case_store import _case_type_for_findings
        findings = [Finding(finding_id="f1", detector_id="waf_attack_success_candidate")]
        assert _case_type_for_findings(findings) == "web_intrusion_candidate"

    def test_default_security_investigation(self) -> None:
        from agent_py_agent.agent.log_analysis.cases.case_store import _case_type_for_findings
        findings = [Finding(finding_id="f1", detector_id="unknown_rule")]
        assert _case_type_for_findings(findings) == "security_investigation"
