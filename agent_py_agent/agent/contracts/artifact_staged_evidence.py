# LLM: Staged evidence adapters keep final artifact validation tied to source checkpoint evidence.
# 模块用途: 把阶段 source JSON 的证据合同 findings 转成统一 ArtifactFinding，供最终 xlsx/pdf 验收复用。

from __future__ import annotations

import json
from pathlib import Path

from .artifact_acceptance_models import ArtifactFinding
from .staged_checkpoint_acceptance import (
    StagedEvidenceOptions,
    StagedEvidenceRequest,
    staged_json_evidence_findings,
)


# LLM: staged_source_evidence_findings 是 agent_py_agent/agent/contracts/artifact_staged_evidence.py 的结构化 helper；修改时保持不读取普通自然语言作为机器事实。
# 函数用途: 处理 staged source evidence findings 相关的结构化数据、路径或 finding，供当前合同链路调用。
def staged_source_evidence_findings(
    validation_contract: dict[str, object],
    workspace_root: Path,
) -> list[ArtifactFinding]:
    evidence_contract = validation_contract.get("evidence_contract")
    staging = validation_contract.get("staging_contract")
    source_ref = staging.get("source_json_ref") if isinstance(staging, dict) else ""
    if not isinstance(evidence_contract, dict) or not source_ref:
        return []
    return _evidence_finding_records(
        staged_json_evidence_findings(
            StagedEvidenceRequest(
                str(source_ref),
                workspace_root,
                evidence_contract,
                StagedEvidenceOptions(phase="final"),
            )
        )
    )


# LLM: _evidence_finding_records 是 agent_py_agent/agent/contracts/artifact_staged_evidence.py 的结构化 helper；修改时保持不读取普通自然语言作为机器事实。
# 函数用途: 处理 evidence finding records 相关的结构化数据、路径或 finding，供当前合同链路调用。
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
