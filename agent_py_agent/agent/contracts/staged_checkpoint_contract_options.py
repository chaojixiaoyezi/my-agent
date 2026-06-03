
from __future__ import annotations

from dataclasses import dataclass

from ..common.value_parsing import sequence_strings


@dataclass(frozen=True)
class StagedCheckpointContext:
    ref: str
    required_columns: list[str]
    required_sheets_min: int
    evidence_contract: dict[str, object]
    validation_contract: dict[str, object]


def staged_checkpoint_contexts(
    item: dict[str, object],
    preferred_paths: set[str],
) -> list[StagedCheckpointContext]:
    contract = item.get("validation_contract")
    staging = contract.get("staging_contract") if isinstance(contract, dict) else None
    refs = staging.get("checkpoint_refs") if isinstance(staging, dict) else None
    if not isinstance(contract, dict) or not isinstance(staging, dict) or not isinstance(refs, list):
        return []
    source_ref = str(staging.get("source_json_ref") or staging.get("source_ref") or "").strip()
    return [
        StagedCheckpointContext(
            ref=ref_text,
            required_columns=sequence_strings(contract.get("required_columns")),
            required_sheets_min=_positive_int(contract.get("required_sheets_min")),
            evidence_contract=_evidence_contract(contract, ref_text, source_ref),
            validation_contract=dict(contract),
        )
        for ref in refs
        if (ref_text := str(ref).strip()) and ref_text not in preferred_paths
    ]


def _evidence_contract(contract: dict[str, object], ref: str, source_ref: str) -> dict[str, object]:
    if source_ref and ref != source_ref:
        return {}
    evidence = contract.get("evidence_contract")
    result = dict(evidence) if isinstance(evidence, dict) else {}
    metrics = contract.get("metric_contracts")
    if isinstance(metrics, list):
        result["metric_contracts"] = [dict(item) for item in metrics if isinstance(item, dict)]
    return result


def _positive_int(value: object) -> int:
    try:
        parsed = int(value or 0)
    except (TypeError, ValueError):
        return 0
    return max(parsed, 0)


__all__ = ["StagedCheckpointContext", "staged_checkpoint_contexts"]
