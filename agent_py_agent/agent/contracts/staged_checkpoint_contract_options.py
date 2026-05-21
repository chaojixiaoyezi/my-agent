# LLM: staged checkpoint contract options normalize validation_contract fields for checkpoint checks.
# 模块用途: 从 artifact validation_contract 提取每个 checkpoint 的列数、sheet 数和证据合同。

from __future__ import annotations

from dataclasses import dataclass


# LLM: StagedCheckpointContext stores structured runtime facts for the surrounding contract logic.
# 类用途: 保存当前模块使用的结构化字段，避免后续流程从普通自然语言推断机器事实。
@dataclass(frozen=True)
class StagedCheckpointContext:
    ref: str
    required_columns: list[str]
    required_sheets_min: int
    evidence_contract: dict[str, object]
    validation_contract: dict[str, object]


# LLM: staged_checkpoint_contexts binds each staged ref to the artifact validation options it must satisfy.
# 函数用途: 生成 checkpoint 检查上下文，避免 acceptance 和 closeout 对 staged JSON 使用两套规则。
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
            required_columns=_string_list(contract.get("required_columns")),
            required_sheets_min=_positive_int(contract.get("required_sheets_min")),
            evidence_contract=_evidence_contract(contract, ref_text, source_ref),
            validation_contract=dict(contract),
        )
        for ref in refs
        if (ref_text := str(ref).strip()) and ref_text not in preferred_paths
    ]


# LLM: _evidence_contract keeps this runtime helper grounded in structured fields.
# 函数用途: 处理当前模块的结构化数据流，不把普通自然语言文本当作系统事实来源。
def _evidence_contract(contract: dict[str, object], ref: str, source_ref: str) -> dict[str, object]:
    if source_ref and ref != source_ref:
        return {}
    evidence = contract.get("evidence_contract")
    return dict(evidence) if isinstance(evidence, dict) else {}


# LLM: _string_list keeps this runtime helper grounded in structured fields.
# 函数用途: 处理当前模块的结构化数据流，不把普通自然语言文本当作系统事实来源。
def _string_list(value: object) -> list[str]:
    return [text for item in value if (text := str(item).strip())] if isinstance(value, list) else []


# LLM: _positive_int keeps this runtime helper grounded in structured fields.
# 函数用途: 处理当前模块的结构化数据流，不把普通自然语言文本当作系统事实来源。
def _positive_int(value: object) -> int:
    try:
        parsed = int(value or 0)
    except (TypeError, ValueError):
        return 0
    return max(parsed, 0)


__all__ = ["StagedCheckpointContext", "staged_checkpoint_contexts"]
