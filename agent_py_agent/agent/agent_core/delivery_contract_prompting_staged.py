# LLM: Staged JSON recovery prompting; keep checkpoint repair hints generic and contract-derived.
# 模块用途: 渲染 staged JSON 缺行、坏 JSON、collection/evidence 合同修复提示。

from __future__ import annotations

import json

from .delivery_contract_prompting_recovery_bool import _json_bool


# LLM: _staged_json_no_rows_lines renders the structured data handoff after an empty JSON checkpoint.
# 函数用途: 当阶段 JSON 无行时，提示模型补非空 rows/sheets，再调用合同里的 builder_tool 生成产物。
def _staged_json_no_rows_lines(
    finding: dict[str, object], artifact_items: list[dict[str, object]]
) -> list[str]:
    stage_ref = str(finding.get("stage_ref") or finding.get("location") or "")
    artifact = _artifact_for_stage_ref(artifact_items, stage_ref)
    validation = artifact.get("validation_contract") if isinstance(artifact.get("validation_contract"), dict) else {}
    staging = validation.get("staging_contract") if isinstance(validation.get("staging_contract"), dict) else {}
    columns = validation.get("required_columns") if isinstance(validation.get("required_columns"), list) else []
    lines = [
        "- staged_json_no_rows:",
        f"  - source_ref={_staging_source_ref(staging) or stage_ref}",
        f"  - write_shape={_checkpoint_shape_hint(stage_ref, staging)}",
        f"  - required_columns={', '.join(str(item) for item in columns)}",
        f"  - writer_tool={finding.get('writer_tool') or 'write_structured_json'}",
        f"  - builder_tool={staging.get('builder_tool') or ''}",
        f"  - output_ref={_staging_output_ref(staging)}",
        "  - 优先用 writer_tool 写 path/rows/sheets/data，避免手写大型 JSON 字符串。",
        "  - 如果 min_items_total 很大，优先用 writer_tool 的 generated_rows 结构化参数生成 rows 和多 sheet。",
        "  - 如果数据来自多个 JSON API，可用 api_json_collection 一次生成带 source_refs/claims 的结构化 JSON；"
        "大量同形日期/分页请求优先用 request_ranges，避免手写长 JSON。",
        "  - source_ref 有非空 rows/sheets 或声明形状后，再调用 builder_tool；不要把空 checkpoint 当完成。",
    ]
    lines.extend(_collection_contract_lines(validation))
    return lines


# LLM: _staged_json_invalid_lines renders checkpoint-repair guidance from structured refs only.
# 函数用途: 当阶段 JSON 语法坏掉或被截断时，提示模型先修复结构化 JSON，再继续 builder/下一阶段产物。
def _staged_json_invalid_lines(
    finding: dict[str, object], artifact_items: list[dict[str, object]]
) -> list[str]:
    stage_ref = str(finding.get("stage_ref") or finding.get("location") or "")
    artifact = _artifact_for_stage_ref(artifact_items, stage_ref)
    validation = artifact.get("validation_contract") if isinstance(artifact.get("validation_contract"), dict) else {}
    staging = validation.get("staging_contract") if isinstance(validation.get("staging_contract"), dict) else {}
    parse_error = str(finding.get("parse_error") or "")
    return [
        "- staged_json_invalid:",
        f"  - source_ref={_staging_source_ref(staging) or stage_ref}",
        f"  - required_shape={_checkpoint_shape_hint(stage_ref, staging)}",
        f"  - parse_error={json.dumps(parse_error, ensure_ascii=False)}",
        f"  - writer_tool={finding.get('writer_tool') or 'write_structured_json'}",
        f"  - builder_tool={staging.get('builder_tool') or ''}",
        f"  - output_ref={_staging_output_ref(staging)}",
        "  - 优先用 writer_tool 重写 path/rows/sheets/data，避免手动修补截断 JSON。",
        "  - 先把 source_ref 修成可解析的完整 checkpoint，再继续 builder_tool 或下一阶段产物。",
    ]


# LLM: _artifact_for_stage_ref finds the artifact contract that owns a staged checkpoint ref.
# 函数用途: 用 staging_contract.checkpoint_refs/source_json_ref 匹配 artifact，不根据提示词或 stdout 推断。
def _artifact_for_stage_ref(
    artifact_items: list[dict[str, object]], stage_ref: str
) -> dict[str, object]:
    for artifact in artifact_items:
        validation = artifact.get("validation_contract") if isinstance(artifact.get("validation_contract"), dict) else {}
        staging = validation.get("staging_contract") if isinstance(validation.get("staging_contract"), dict) else {}
        refs = staging.get("checkpoint_refs") if isinstance(staging.get("checkpoint_refs"), list) else []
        if stage_ref in refs or stage_ref in _staging_refs(staging):
            return artifact
    return {}


# LLM: _staging_refs returns source/output aliases that can identify an artifact's staged contract.
# 函数用途: 让恢复提示支持 JSON、Markdown、PDF、workbook 等通用 ref，不再只匹配 source_json_ref。
def _staging_refs(staging: dict[str, object]) -> set[str]:
    return {
        str(staging.get(key) or "").strip()
        for key in _staging_ref_keys(staging)
        if str(staging.get(key) or "").strip()
    }


def _staging_source_ref(staging: dict[str, object]) -> str:
    for key in _staging_source_keys(staging):
        if value := str(staging.get(key) or "").strip():
            return value
    return ""


def _staging_output_ref(staging: dict[str, object]) -> str:
    for key in _staging_output_keys(staging):
        if value := str(staging.get(key) or "").strip():
            return value
    return ""


def _staging_ref_keys(staging: dict[str, object]) -> tuple[str, ...]:
    return (*_staging_source_keys(staging), *_staging_output_keys(staging))


def _staging_source_keys(staging: dict[str, object]) -> tuple[str, ...]:
    keys = [
        str(staging.get("source_ref_key") or "").strip(),
        str(staging.get("input_ref_key") or "").strip(),
        "source_json_ref",
        "source_markdown_ref",
        "source_ref",
        "input_ref",
    ]
    return tuple(dict.fromkeys(key for key in keys if key))


def _staging_output_keys(staging: dict[str, object]) -> tuple[str, ...]:
    keys = [
        str(staging.get("output_ref_key") or "").strip(),
        "workbook_ref",
        "pdf_ref",
        "output_ref",
        "artifact_ref",
    ]
    return tuple(dict.fromkeys(key for key in keys if key))


# LLM: _collection_contract_lines exposes generic coverage and evidence facts from the machine contract.
# 函数用途: 从 collection_contract/evidence_contract 渲染最小组数、每组条数和证据要求，不从任务文案推断。
def _collection_contract_lines(validation: dict[str, object]) -> list[str]:
    collection = validation.get("collection_contract")
    evidence = validation.get("evidence_contract")
    lines: list[str] = []
    if isinstance(collection, dict):
        lines.extend(_collection_shape_lines(collection))
        lines.extend(_item_evidence_contract_lines(collection, evidence))
    if isinstance(evidence, dict):
        lines.extend(_evidence_contract_lines(evidence))
    return lines


# LLM: _collection_shape_lines 是 agent_py_agent/agent/agent_core/delivery_contract_prompting_recovery.py 的结构化 helper；修改时保持不读取普通自然语言作为机器事实。
# 函数用途: 处理 collection shape lines 相关的结构化数据、路径或 finding，供当前合同链路调用。
def _collection_shape_lines(collection: dict[str, object]) -> list[str]:
    lines = [f"  - {key}={value}" for key in ("groups_path", "items_path", "min_groups", "min_items_per_group") if (value := collection.get(key))]
    fields = collection.get("required_item_fields")
    if isinstance(fields, list) and fields:
        lines.append(f"  - required_item_fields={', '.join(str(item) for item in fields)}")
    return lines


# LLM: _evidence_contract_lines 是 agent_py_agent/agent/agent_core/delivery_contract_prompting_recovery.py 的结构化 helper；修改时保持不读取普通自然语言作为机器事实。
# 函数用途: 处理 evidence contract lines 相关的结构化数据、路径或 finding，供当前合同链路调用。
def _evidence_contract_lines(evidence: dict[str, object]) -> list[str]:
    lines: list[str] = []
    if evidence.get("require_verified") is not None:
        lines.append(f"  - require_verified_evidence={_json_bool(evidence.get('require_verified'), default=False)}")
    fields = evidence.get("required_fields")
    if isinstance(fields, list) and fields:
        lines.append(f"  - evidence_required_fields={', '.join(str(item) for item in fields)}")
    return lines


# LLM: _item_evidence_contract_lines 是 agent_py_agent/agent/agent_core/delivery_contract_prompting_recovery.py 的结构化 helper；修改时保持不读取普通自然语言作为机器事实。
# 函数用途: 处理 item evidence contract lines 相关的结构化数据、路径或 finding，供当前合同链路调用。
def _item_evidence_contract_lines(collection: dict[str, object], evidence: object) -> list[str]:
    evidence_fields = collection.get("required_item_evidence_fields")
    if not isinstance(evidence_fields, list) and isinstance(evidence, dict) and collection.get("require_item_evidence") is not False:
        evidence_fields = evidence.get("required_fields")
    if not isinstance(evidence_fields, list) or not evidence_fields:
        return []
    return [
        f"  - item_evidence_required_fields={', '.join(str(item) for item in evidence_fields)}",
        "  - item_evidence_shape=field_source_ids 或 row-scoped claims.reserved.item_path",
    ]


# LLM: _checkpoint_shape_hint lets each staged checkpoint describe its own generic JSON shape without hard-coding one task's schema.
# 函数用途: 优先读取 staging_contract.checkpoint_shape_hints；缺失时回退到宽松通用 JSON 形状提示。
def _checkpoint_shape_hint(stage_ref: str, staging: dict[str, object]) -> str:
    hints = staging.get("checkpoint_shape_hints")
    if isinstance(hints, dict):
        hint = str(hints.get(stage_ref) or "").strip()
        if hint:
            return hint
    return "[] 或 {\"rows\":[...]} 或 {\"sheets\":[{\"name\":\"...\",\"rows\":[...]}]} 这类非空结构化 JSON"
