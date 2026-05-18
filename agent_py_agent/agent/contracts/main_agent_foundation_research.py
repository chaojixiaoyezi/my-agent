# LLM: Main-agent research evidence fixtures live outside the foundation runner to keep the matrix thin.
# 模块用途: 生成资料证据合同测试结果，证明关键统计字段必须有结构化来源引用。

from __future__ import annotations

import json
from pathlib import Path

from .evidence_contract import (
    EvidenceClaim,
    EvidenceContractRequest,
    EvidenceSourceRef,
    evaluate_evidence_contract,
)


# LLM: research_evidence_contract_case returns a dict compatible with MainAgentFoundationCaseResult.
# 函数用途: 用一正一反两组资料 claim 证明关键统计字段必须挂 source_ref，供主 runner 直接包装。
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


# LLM: _valid_research_evidence_contract builds the positive sourced-claim fixture.
# 函数用途: 构造带 source_ref 的 GitHub 统计 claim，证明有来源资料可以通过证据合同。
def _valid_research_evidence_contract():
    return evaluate_evidence_contract(
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
                )
            ],
            required_fields=["stargazers_count"],
        )
    )


# LLM: _invalid_research_evidence_contract builds the negative unsourced-claim fixture.
# 函数用途: 构造没有 source_ref 的增长数据 claim，证明模型不能凭空填写关键统计。
def _invalid_research_evidence_contract():
    return evaluate_evidence_contract(
        EvidenceContractRequest(
            claims=[
                EvidenceClaim(
                    claim_id="repo-weekly-growth",
                    field="weekly_star_growth",
                    value=581200,
                    source_ids=[],
                )
            ],
            required_fields=["weekly_star_growth"],
        )
    )


# LLM: _write_research_evidence_report persists evidence-contract outputs as refs.
# 函数用途: 写出有效/无效两组证据合同结果，供主代理基础测试报告引用。
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


# LLM: _research_evidence_issues turns contract booleans into stable test issue labels.
# 函数用途: 根据正例/反例的 ok 状态输出问题列表，避免测试结果解析自然语言。
def _research_evidence_issues(valid_ok: bool, invalid_ok: bool) -> list[str]:
    issues: list[str] = []
    if not valid_ok:
        issues.append("valid sourced claim failed")
    if invalid_ok:
        issues.append("unsourced claim passed")
    return issues

