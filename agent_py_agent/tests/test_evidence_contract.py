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


# LLM: The shared evidence primitive should not force final-delivery verification by default.
# 函数用途: 验证基础证据合同默认只要求有来源；最终交付严格性由上层合同显式 require_verified 控制。
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


# LLM: Estimated facts are acceptable only when the contract explicitly allows them and the method is structured.
# 函数用途: 验证真实世界拿不到精确值时，可以用 estimated claim，但必须有来源、方法和置信度，不靠正文解释。
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
                    methodology="weekly ranking overlap plus current GitHub snapshot",
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


# LLM: Estimates without a machine-readable method should not satisfy numeric evidence contracts.
# 函数用途: 验证模型不能只在表格或说明里写“估算”，必须把估算口径写入 claim 机器字段。
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
