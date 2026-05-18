"""Focused tests for machine-readable research evidence contracts."""

from __future__ import annotations


# LLM: Sourced research claims should pass without relying on prose summaries.
# 函数用途: 验证每个数据字段都有结构化来源时，资料证据合同通过。
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
                    source_id="github-api-openclaw",
                    source_type="api",
                    uri="https://api.github.com/repos/openclaw/openclaw",
                    retrieved_at="2026-05-18T10:00:00Z",
                )
            ],
            claims=[
                EvidenceClaim(
                    claim_id="openclaw-stars",
                    field="stargazers_count",
                    value=372838,
                    source_ids=["github-api-openclaw"],
                    verification_status="VERIFIED",
                )
            ],
            required_fields=["stargazers_count"],
        )
    )

    assert report.ok is True
    assert report.findings == []
    assert report.to_dict()["summary"]["verified_claims"] == 1


# LLM: Generated tables must not pass when key numeric fields have no source ref.
# 函数用途: 固定 GitHub star XLSX 真实测试暴露的问题；没有证据的增长数字不能当真。
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


# LLM: Unknown or unverifiable sources must fail as machine facts, not become warnings in prose.
# 函数用途: 验证引用不存在 source_id 或缺少可读取 URI/artifact_ref 时，合同给出硬失败。
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
