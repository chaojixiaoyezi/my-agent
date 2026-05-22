from __future__ import annotations

import json
from pathlib import Path

from agent_py_agent.agent.contracts.gates.delivery_quality import (
    DeliveryQualityTraceScope,
    append_delivery_quality_gate_trace,
    evaluate_delivery_quality_gate,
)


# LLM: Delivery quality gates must reject wrong metric semantics, not only missing evidence.
# 函数用途: 验证时间窗口增量不能由当前总量冒充，机器只读 metric_kind 结构字段。
def test_delivery_quality_gate_rejects_time_window_delta_from_point_in_time_total() -> None:
    decision = evaluate_delivery_quality_gate(
        _payload(
            claims=[
                {
                    "claim_id": "claim-growth",
                    "field": "growth_count",
                    "value": 4210,
                    "source_ids": ["src-current"],
                    "verification_status": "VERIFIED",
                    "value_type": "exact",
                    "reserved": {"metric_kind": "point_in_time_total"},
                }
            ],
            source_refs=[
                {
                    "source_id": "src-current",
                    "uri": "https://api.example.invalid/repositories",
                    "reserved": {"metric_kind": "point_in_time_total"},
                }
            ],
        ),
        _contract(
            metric_contracts=[
                {
                    "field": "growth_count",
                    "expected_kind": "time_window_delta",
                    "required_window": True,
                }
            ]
        ),
        contract_hash="sha256:metric-contract",
    )

    assert decision.allowed is False
    assert "METRIC_KIND_MISMATCH" in decision.finding_codes


# LLM: Estimated metrics may be allowed only when their uncertainty is structured.
# 函数用途: 验证估算值即使有 methodology，也必须带 limitations 才能通过严格口径合同。
def test_delivery_quality_gate_requires_limitations_for_estimated_metrics() -> None:
    decision = evaluate_delivery_quality_gate(
        _estimated_metric_payload(),
        _estimated_metric_contract(),
        contract_hash="sha256:metric-contract",
    )

    assert decision.allowed is False
    assert "METRIC_ESTIMATE_LIMITATIONS_MISSING" in decision.finding_codes


# LLM: Language quality checks are driven by structured field contracts, not prompt keywords.
# 函数用途: 验证声明为中文交付字段时，英文占位内容不能通过质量门。
def test_delivery_quality_gate_rejects_non_target_language_rows() -> None:
    decision = evaluate_delivery_quality_gate(
        {
            "items": [
                {
                    "name": "demo",
                    "summary_zh": "Fast modern database platform",
                }
            ],
            "source_refs": [{"source_id": "src-1", "uri": "https://example.invalid/demo"}],
            "claims": [
                {
                    "claim_id": "claim-summary",
                    "field": "summary_zh",
                    "value": "Fast modern database platform",
                    "source_ids": ["src-1"],
                    "verification_status": "VERIFIED",
                }
            ],
        },
        _contract(
            evidence_contract={"required_fields": ["summary_zh"], "require_verified": True},
            language_contract={
                "target_language": "zh",
                "fields": ["summary_zh"],
                "min_cjk_chars": 4,
                "max_latin_ratio": 0.45,
            },
        ),
        contract_hash="sha256:language-contract",
    )

    assert decision.allowed is False
    assert decision.finding_codes == ("LANGUAGE_FIELD_TARGET_MISMATCH",)


# LLM: Artifact validation must be tied to the same effective contract hash used for closeout.
# 函数用途: 验证旧合同验收过的产物不能在新合同下直接收口。
def test_delivery_quality_gate_rejects_stale_artifact_contract_hash() -> None:
    decision = evaluate_delivery_quality_gate(
        {
            "artifacts": [
                {
                    "artifact_ref": "outputs/report.xlsx",
                    "hash": "artifact-sha",
                    "validated_contract_hash": "sha256:old-contract",
                }
            ],
            "source_refs": [{"source_id": "src-1", "uri": "https://example.invalid"}],
            "claims": [
                {
                    "claim_id": "claim-title",
                    "field": "title",
                    "value": "已核验标题",
                    "source_ids": ["src-1"],
                    "verification_status": "VERIFIED",
                }
            ],
        },
        _contract(evidence_contract={"required_fields": ["title"], "require_verified": True}),
        contract_hash="sha256:new-contract",
    )

    assert decision.allowed is False
    assert decision.finding_codes == ("ARTIFACT_VALIDATION_CONTRACT_HASH_MISMATCH",)


# LLM: Delivery quality decisions should leave an append-only trace for replay and audit.
# 函数用途: 验证质量门结果可落 gate trace，后续真实 run 能复盘哪一门挡住。
def test_delivery_quality_gate_trace_records_contract_hash_and_findings(tmp_path: Path) -> None:
    trace_path = tmp_path / "delivery_quality_gate.jsonl"
    decision = evaluate_delivery_quality_gate(
        _payload(claims=[], source_refs=[]),
        _contract(evidence_contract={"required_fields": ["title"], "require_verified": True}),
        contract_hash="sha256:trace-contract",
    )

    append_delivery_quality_gate_trace(
        trace_path,
        decision,
        DeliveryQualityTraceScope(
            run_id="run-1",
            task_id="task-1",
            contract_hash="sha256:trace-contract",
        ),
    )

    records = [json.loads(line) for line in trace_path.read_text(encoding="utf-8").splitlines()]
    assert records == [
        {
            "event_type": "delivery_quality_gate",
            "run_id": "run-1",
            "task_id": "task-1",
            "contract_hash": "sha256:trace-contract",
            "gate": "delivery_quality",
            "status": "NEED_REPAIR",
            "allowed": False,
            "finding_codes": ["EVIDENCE_REQUIRED_FIELD_MISSING"],
        }
    ]


def _payload(
    *,
    claims: list[dict[str, object]],
    source_refs: list[dict[str, object]],
) -> dict[str, object]:
    return {
        "source_refs": source_refs,
        "claims": claims,
        "items": [],
    }


def _contract(
    *,
    evidence_contract: dict[str, object] | None = None,
    metric_contracts: list[dict[str, object]] | None = None,
    language_contract: dict[str, object] | None = None,
) -> dict[str, object]:
    return {
        "evidence_contract": evidence_contract
        or {
            "required_fields": ["growth_count"],
            "allowed_value_types": ["exact"],
            "require_verified": True,
        },
        "metric_contracts": metric_contracts or [],
        "language_contract": language_contract or {},
    }


def _estimated_metric_payload() -> dict[str, object]:
    return _payload(
        claims=[
            {
                "claim_id": "claim-growth",
                "field": "growth_count",
                "value": "~100-120",
                "source_ids": ["src-ranking"],
                "verification_status": "VERIFIED",
                "value_type": "estimated",
                "methodology": "ranking snapshot plus repository current counters",
                "reserved": {
                    "metric_kind": "time_window_delta",
                    "window_start": "2026-05-11",
                    "window_end": "2026-05-17",
                },
            }
        ],
        source_refs=[
            {
                "source_id": "src-ranking",
                "uri": "https://example.invalid/ranking",
                "reserved": {"metric_kind": "time_window_delta"},
            }
        ],
    )


def _estimated_metric_contract() -> dict[str, object]:
    return _contract(
        metric_contracts=[
            {
                "field": "growth_count",
                "expected_kind": "time_window_delta",
                "required_window": True,
                "allow_estimated": True,
                "require_limitations_for_estimates": True,
            }
        ],
        evidence_contract={
            "required_fields": ["growth_count"],
            "allowed_value_types": ["exact", "estimated"],
            "require_methodology_for_estimates": True,
            "require_verified": True,
        },
    )
