# LLM: Staged evidence adapters keep final artifact validation tied to source checkpoint evidence.
# 模块用途: 把阶段 source JSON 的证据合同 findings 转成统一 ArtifactFinding，供最终 xlsx/pdf 验收复用。

from __future__ import annotations

import json
from pathlib import Path

from .artifact_acceptance_models import ArtifactFinding
from .staged_checkpoint_acceptance import staged_json_evidence_findings


def staged_source_evidence_findings(
    validation_contract: dict[str, object],
    workspace_root: Path,
) -> list[ArtifactFinding]:
    evidence_contract = validation_contract.get("evidence_contract")
    staging = validation_contract.get("staging_contract")
    source_ref = staging.get("source_json_ref") if isinstance(staging, dict) else ""
    if not isinstance(evidence_contract, dict) or not source_ref:
        return []
    return _evidence_finding_records(staged_json_evidence_findings(str(source_ref), workspace_root, evidence_contract))


def _evidence_finding_records(items: list[dict[str, object]]) -> list[ArtifactFinding]:
    records: list[ArtifactFinding] = []
    public_keys = {"code", "severity", "message", "location", "value"}
    for item in items:
        details = {key: value for key, value in item.items() if key not in public_keys}
        value = item.get("value")
        if value is None and details:
            value = json.dumps(details, ensure_ascii=False, sort_keys=True)
        records.append(
            ArtifactFinding(
                code=str(item.get("code") or ""),
                severity=str(item.get("severity") or "hard"),
                message=str(item.get("message") or ""),
                location=str(item.get("location") or ""),
                value=str(value or ""),
            )
        )
    return records


__all__ = ["staged_source_evidence_findings"]
