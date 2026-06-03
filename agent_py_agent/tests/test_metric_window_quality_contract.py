from __future__ import annotations

from agent_py_agent.agent.contracts.evidence_contract import EvidenceClaim, EvidenceSourceRef
from agent_py_agent.agent.contracts.gates.delivery_quality import (
    delivery_quality_metric_findings,
)


def test_metric_window_contract_rejects_inconsistent_claim_windows():
    findings = delivery_quality_metric_findings(
        [
            {
                "field": "star_delta",
                "expected_kind": "period_delta",
                "required_window": True,
                "require_consistent_window": True,
            }
        ],
        [],
        [
            EvidenceClaim(
                "c1",
                "star_delta",
                10,
                reserved={"metric_kind": "period_delta", "window_start": "2026-01-01", "window_end": "2026-01-07"},
            ),
            EvidenceClaim(
                "c2",
                "star_delta",
                11,
                reserved={"metric_kind": "period_delta", "window_start": "2026-01-08", "window_end": "2026-01-14"},
            ),
        ],
    )

    assert [item.code for item in findings] == ["METRIC_WINDOW_INCONSISTENT"]


def test_metric_window_contract_accepts_source_window_when_claim_omits_it():
    findings = delivery_quality_metric_findings(
        [{"field": "star_delta", "expected_kind": "period_delta", "required_window": True}],
        [
            EvidenceSourceRef(
                "src-1",
                uri="https://example.com/data.json",
                reserved={"metric_kind": "period_delta", "time_window": {"start": "2026-01-01", "end": "2026-01-07"}},
            )
        ],
        [EvidenceClaim("c1", "star_delta", 10, source_ids=["src-1"])],
    )

    assert findings == []
