
from __future__ import annotations

import json
from pathlib import Path

from .evidence_contract import (
    EvidenceClaim,
    EvidenceContractRequest,
    EvidenceSourceRef,
    evaluate_evidence_contract,
)


def research_evidence_contract_case(workspace: Path) -> dict[str, object]:
    valid = _valid_research_evidence_contract()
    invalid = _invalid_research_evidence_contract()
    evidence = _write_research_evidence_report(workspace, valid, invalid)
    issues = _research_evidence_issues(valid.ok, invalid.ok)
    return {
        "case_id": "research_evidence_contracts",
        "title": "资料证据合同测试",
        "status": "FAILED" if issues else "PASSED",
        "summary": "关键资料字段必须有 source_ref；无来源统计不能通过验收。",
        "evidence_refs": [str(evidence)],
        "issues": issues,
    }


def _valid_research_evidence_contract():
    return evaluate_evidence_contract(
        EvidenceContractRequest(
            source_refs=[
                EvidenceSourceRef(
                    source_id="source-api-example",
                    source_type="api",
                    uri="https://example.com/data/project.json",
                    retrieved_at="2026-05-18T10:00:00Z",
                )
            ],
            claims=[
                EvidenceClaim(
                    claim_id="sourced-metric-value",
                    field="metric_value",
                    value=372838,
                    source_ids=["source-api-example"],
                )
            ],
            required_fields=["metric_value"],
        )
    )


def _invalid_research_evidence_contract():
    return evaluate_evidence_contract(
        EvidenceContractRequest(
            claims=[EvidenceClaim(claim_id="unsourced-metric-value", field="metric_delta", value=581200, source_ids=[])],
            required_fields=["metric_delta"],
        )
    )


def _write_research_evidence_report(workspace: Path, valid, invalid) -> Path:
    evidence = workspace / "research_evidence_contracts" / "report.json"
    evidence.parent.mkdir(parents=True, exist_ok=True)
    evidence.write_text(
        json.dumps(
            {"valid": valid.to_dict(), "invalid": invalid.to_dict()},
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    return evidence


def _research_evidence_issues(valid_ok: bool, invalid_ok: bool) -> list[str]:
    issues: list[str] = []
    if not valid_ok:
        issues.append("valid sourced claim failed")
    if invalid_ok:
        issues.append("unsourced claim passed")
    return issues
