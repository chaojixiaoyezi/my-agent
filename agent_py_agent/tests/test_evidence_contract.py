"""Focused tests for machine-readable research evidence contracts."""

from __future__ import annotations


def test_evidence_contract_accepts_sourced_claims():
    from agent_py_agent.agent.contracts.evidence_contract import (
        EvidenceClaim,
        EvidenceContractRequest,
        EvidenceSourceRef,
        evaluate_evidence_contract,
    )

    report = evaluate_evidence_contract(
        EvidenceContractRequest(
            source_refs=[
                EvidenceSourceRef(
                    source_id="github-api-sample-c",
                    source_type="api",
                    uri="https://api.github.com/repos/sample-c/sample-c",
                    retrieved_at="2026-05-18T10:00:00Z",
                )
            ],
            claims=[
                EvidenceClaim(
                    claim_id="sample-c-stars",
                    field="stargazers_count",
                    value=372838,
                    source_ids=["github-api-sample-c"],
                    verification_status="VERIFIED",
                )
            ],
            required_fields=["stargazers_count"],
        )
    )

    assert report.ok is True
    assert report.findings == []
    assert report.to_dict()["summary"]["verified_claims"] == 1


def test_evidence_contract_default_allows_sourced_pending_claims():
    from agent_py_agent.agent.contracts.evidence_contract import (
        EvidenceClaim,
        EvidenceContractRequest,
        EvidenceSourceRef,
        evaluate_evidence_contract,
    )

    report = evaluate_evidence_contract(
        EvidenceContractRequest(
            source_refs=[EvidenceSourceRef(source_id="src-1", uri="https://example.com")],
            claims=[
                EvidenceClaim(
                    claim_id="claim-1",
                    field="weekly_star_growth",
                    value=120,
                    source_ids=["src-1"],
                    verification_status="PENDING",
                )
            ],
            required_fields=["weekly_star_growth"],
        )
    )
    strict_report = evaluate_evidence_contract(
        EvidenceContractRequest(
            source_refs=[EvidenceSourceRef(source_id="src-1", uri="https://example.com")],
            claims=[
                EvidenceClaim(
                    claim_id="claim-1",
                    field="weekly_star_growth",
                    value=120,
                    source_ids=["src-1"],
                    verification_status="PENDING",
                )
            ],
            required_fields=["weekly_star_growth"],
            require_verified=True,
        )
    )

    assert report.ok is True
    assert strict_report.ok is False
    assert [item["code"] for item in strict_report.findings] == ["EVIDENCE_CLAIM_UNVERIFIED"]


def test_evidence_contract_rejects_unsourced_required_claims():
    from agent_py_agent.agent.contracts.evidence_contract import (
        EvidenceClaim,
        EvidenceContractRequest,
        evaluate_evidence_contract,
    )

    report = evaluate_evidence_contract(
        EvidenceContractRequest(
            claims=[
                EvidenceClaim(
                    claim_id="repo-weekly-growth",
                    field="weekly_star_growth",
                    value=581200,
                    source_ids=[],
                    verification_status="VERIFIED",
                )
            ],
            required_fields=["weekly_star_growth"],
        )
    )

    assert report.ok is False
    assert [item["code"] for item in report.findings] == ["EVIDENCE_CLAIM_UNSOURCED"]


def test_evidence_contract_rejects_missing_or_unreadable_sources():
    from agent_py_agent.agent.contracts.evidence_contract import (
        EvidenceClaim,
        EvidenceContractRequest,
        EvidenceSourceRef,
        evaluate_evidence_contract,
    )

    report = evaluate_evidence_contract(
        EvidenceContractRequest(
            source_refs=[EvidenceSourceRef(source_id="empty-source", source_type="api")],
            claims=[
                EvidenceClaim(
                    claim_id="repo-stars",
                    field="stargazers_count",
                    value=100,
                    source_ids=["missing-source", "empty-source"],
                    verification_status="VERIFIED",
                )
            ],
            required_fields=["stargazers_count"],
        )
    )

    assert report.ok is False
    assert [item["code"] for item in report.findings] == [
        "EVIDENCE_SOURCE_UNREADABLE",
        "EVIDENCE_SOURCE_MISSING",
    ]


def test_evidence_contract_accepts_declared_estimates_with_methodology():
    from agent_py_agent.agent.contracts.evidence_contract import (
        EvidenceClaim,
        EvidenceContractRequest,
        EvidenceSourceRef,
        evaluate_evidence_contract,
    )

    report = evaluate_evidence_contract(
        EvidenceContractRequest(
            source_refs=[
                EvidenceSourceRef(
                    source_id="star-history-weekly",
                    source_type="web",
                    uri="https://www.star-history.com/",
                    retrieved_at="2026-05-22T00:00:00Z",
                )
            ],
            claims=[
                EvidenceClaim(
                    claim_id="repo-weekly-growth-estimate",
                    field="weekly_star_growth",
                    value="~1,500-1,600",
                    source_ids=["star-history-weekly"],
                    confidence=0.78,
                    verification_status="VERIFIED",
                    value_type="estimated",
                    methodology="weekly ranking overlap plus current 代码平台 snapshot",
                )
            ],
            required_fields=["weekly_star_growth"],
            allowed_value_types=["exact", "estimated"],
            min_confidence=0.5,
            require_methodology_for_estimates=True,
        )
    )

    assert report.ok is True
    assert report.findings == []
    claim = report.to_dict()["claims"][0]
    assert claim["value_type"] == "estimated"
    assert claim["methodology"]


def test_evidence_contract_rejects_estimates_without_methodology():
    from agent_py_agent.agent.contracts.evidence_contract import (
        EvidenceClaim,
        EvidenceContractRequest,
        EvidenceSourceRef,
        evaluate_evidence_contract,
    )

    report = evaluate_evidence_contract(
        EvidenceContractRequest(
            source_refs=[
                EvidenceSourceRef(
                    source_id="star-history-weekly",
                    source_type="web",
                    uri="https://www.star-history.com/",
                )
            ],
            claims=[
                EvidenceClaim(
                    claim_id="repo-weekly-growth-estimate",
                    field="weekly_star_growth",
                    value="~1,500-1,600",
                    source_ids=["star-history-weekly"],
                    confidence=0.78,
                    verification_status="VERIFIED",
                    value_type="estimated",
                )
            ],
            required_fields=["weekly_star_growth"],
            allowed_value_types=["exact", "estimated"],
            require_methodology_for_estimates=True,
        )
    )

    assert report.ok is False
    assert [item["code"] for item in report.findings] == ["EVIDENCE_ESTIMATE_METHOD_MISSING"]
