# LLM: Delivery fact evidence bridge wires source/claim verification into closeout.
# 模块用途: 从交付合同加载事实证据 payload，并在最终收口前执行通用 fact_evidence 门。

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ..contracts.gates import GateDecision, GateFinding, evaluate_fact_evidence_gate
from .main_agent_delivery_closeout_quality import (
    delivery_quality_payload_ref,
    workspace_relative_json_path,
)


# LLM: fact_evidence_decision runs only on structured fact-evidence contracts.
# 函数用途: 有 fact_evidence_contract 时加载 source_refs/claims 并校验工具来源绑定；未声明时显式放行。
def fact_evidence_decision(
    *,
    contract: dict[str, Any],
    workspace_root: Path,
    archive_tool_calls: list[dict[str, Any]],
) -> GateDecision:
    fact_contract = fact_evidence_contract(contract)
    if not fact_contract:
        return GateDecision.allow("fact_evidence", evidence={"declared": False})
    payload = load_fact_evidence_payload(contract, workspace_root)
    if payload is None:
        return GateDecision.repair("fact_evidence", [GateFinding("FACT_EVIDENCE_PAYLOAD_MISSING")])
    return evaluate_fact_evidence_gate(payload, fact_contract, archive_tool_calls=archive_tool_calls)


def fact_evidence_contract(contract: dict[str, Any]) -> dict[str, Any]:
    value = contract.get("fact_evidence_contract")
    if isinstance(value, dict):
        return dict(value)
    quality = contract.get("delivery_quality_contract")
    if isinstance(quality, dict) and bool(quality.get("require_tool_backed_sources")):
        return dict(quality)
    return {}


def load_fact_evidence_payload(contract: dict[str, Any], workspace_root: Path) -> dict[str, Any] | None:
    ref = fact_evidence_payload_ref(contract)
    if not ref:
        return None
    path = workspace_relative_json_path(ref, workspace_root)
    if path is None or not path.exists():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return dict(value) if isinstance(value, dict) else None


def fact_evidence_payload_ref(contract: dict[str, Any]) -> str:
    for key in ("fact_evidence_payload_ref", "evidence_payload_ref", "source_data_ref"):
        if ref := str(contract.get(key) or "").strip():
            return ref
    return delivery_quality_payload_ref(contract)


__all__ = [
    "fact_evidence_contract",
    "fact_evidence_decision",
    "fact_evidence_payload_ref",
    "load_fact_evidence_payload",
]
