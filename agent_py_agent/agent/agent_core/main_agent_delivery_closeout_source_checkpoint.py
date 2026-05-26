# LLM: Source-checkpoint recovery fields keep collection evidence rules out of the closeout orchestrator.
# 模块用途: 为缺失的来源型 checkpoint 生成结构化恢复字段，不靠自然语言判断能否写空骨架。

from __future__ import annotations

from typing import Any

from .main_agent_delivery_closeout_checkpoint_quality import checkpoint_writer_fields


# LLM: missing_checkpoint_recovery_hint returns the typed next-step hint for staged checkpoint creation.
# 函数用途: 来源型 checkpoint 要先绑定 source_refs/claims；普通 checkpoint 才允许最小骨架。
def missing_checkpoint_recovery_hint(ref_text: str, validation_contract: dict[str, object]) -> str:
    if source_evidence_checkpoint_ref(ref_text, validation_contract):
        return (
            "缺失的是来源型 checkpoint；优先用采集/转换工具真实物化，"
            "必须带 source_refs、claims、completion_evidence 和行级 field_source_ids，不能写空骨架。"
        )
    return "先把缺失的阶段文件真实写出来，可以先写最小有效骨架，再继续补内容。"


# LLM: checkpoint_materialization_fields selects the writer contract for one missing checkpoint ref.
# 函数用途: 对来源证据 checkpoint 推荐 通用采集/写入，并保留 write_file 的审计字段门。
def checkpoint_materialization_fields(ref_text: str, validation_contract: dict[str, object]) -> dict[str, object]:
    fields = checkpoint_writer_fields(ref_text)
    if not source_evidence_checkpoint_ref(ref_text, validation_contract):
        return fields
    collection = validation_contract.get("collection_contract")
    collection_contract = dict(collection) if isinstance(collection, dict) else {}
    return {
        **fields,
        "collection_contract": collection_contract,
        "checkpoint_materialization_mode": "source_evidence_first",
        "required_columns": _required_source_columns(collection_contract),
        "required_structured_fields": ["source_refs", "claims", "completion_evidence", "field_source_ids"],
        "requires_auditable_source_evidence": True,
        "write_tools": ["write_file"],
        "writer_tool": "write_file",
    }


# LLM: source_evidence_checkpoint_ref checks only structured validation contract fields.
# 函数用途: 判断 checkpoint 是否为 collection/evidence 合同声明的来源文件，不读取 prompt 文案。
def source_evidence_checkpoint_ref(ref_text: str, validation_contract: dict[str, object]) -> bool:
    collection = validation_contract.get("collection_contract")
    if not isinstance(collection, dict):
        return False
    source_ref = str(collection.get("source_json_ref") or "").strip()
    if not source_ref or ref_text != source_ref:
        return False
    evidence = validation_contract.get("evidence_contract")
    return bool(
        isinstance(evidence, dict)
        or collection.get("require_item_evidence")
        or collection.get("required_item_evidence_fields")
        or collection.get("require_completion_evidence")
    )


def _required_source_columns(collection_contract: dict[str, object]) -> list[str]:
    fields = collection_contract.get("required_item_fields")
    columns = [str(item).strip() for item in fields if str(item).strip()] if isinstance(fields, list) else []
    return list(dict.fromkeys(columns))


__all__ = [
    "checkpoint_materialization_fields",
    "missing_checkpoint_recovery_hint",
    "source_evidence_checkpoint_ref",
]
