# LLM: Delivery recovery prompting keeps continuation hints derived from structured recovery findings only.
# 模块用途: 渲染 delivery_contract.recovery / recovery_reconciliation 里的机器恢复事实，不让系统回读 stdout 或自然语言摘要。

from __future__ import annotations

import json


# LLM: render_recovery_guidance_lines renders machine recovery findings without parsing prior stdout prose.
# 函数用途: 将 recovery.acceptance.runtime_findings 里的结构化恢复事实展示给模型，优先处理 open file_write_session。
def render_recovery_guidance_lines(
    contract: dict[str, object], artifact_items: list[dict[str, object]]
) -> list[str]:
    findings = _runtime_findings(contract)
    if not findings:
        return []
    lines = ["恢复要求："]
    aborted_session_ids = _reconciled_aborted_session_ids(contract)
    lines.extend(_reconciliation_guidance_lines(contract))
    grouped_open_sessions = _grouped_open_write_session_ids(findings)
    lines.extend(_duplicate_open_write_session_lines(findings, grouped_open_sessions))
    for finding in findings:
        if str(finding.get("session_id") or "") in aborted_session_ids:
            continue
        if str(finding.get("session_id") or "") in grouped_open_sessions:
            continue
        lines.extend(_one_runtime_finding_lines(finding, artifact_items))
    return lines


# LLM: _runtime_findings extracts only structured recovery findings from the delivery contract.
# 函数用途: 读取 recovery.acceptance.runtime_findings；不读取 stdout、stderr 或自然语言摘要。
def _runtime_findings(contract: dict[str, object]) -> list[dict[str, object]]:
    recovery = contract.get("recovery")
    acceptance = recovery.get("acceptance") if isinstance(recovery, dict) else None
    findings = acceptance.get("runtime_findings") if isinstance(acceptance, dict) else None
    if not isinstance(findings, list):
        return []
    return [dict(item) for item in findings if isinstance(item, dict)]


# LLM: _reconciliation_guidance_lines renders system-side duplicate-session cleanup facts.
# 函数用途: 展示恢复前已保留/已 abort 的 session，不让模型继续使用旧恢复包里的过期 session。
def _reconciliation_guidance_lines(contract: dict[str, object]) -> list[str]:
    groups = _reconciliation_groups(contract)
    lines: list[str] = []
    for group in groups:
        lines.extend(
            [
                "- reconciled_open_file_write_session:",
                f"  - target_path={group.get('target_path') or ''}",
                f"  - kept_session_id={group.get('kept_session_id') or ''}",
                f"  - kept_next_chunk_index={group.get('kept_next_chunk_index', 0)}",
                f"  - kept_received_chunks={', '.join(str(item) for item in group.get('kept_received_chunks', []))}",
                f"  - retired_session_ids={', '.join(str(item) for item in group.get('retired_session_ids', []))}",
                f"  - aborted_session_ids={', '.join(str(item) for item in group.get('aborted_session_ids', []))}",
                f"  - already_closed_session_ids={', '.join(str(item) for item in group.get('already_closed_session_ids', []))}",
            ]
        )
    return lines


# LLM: _reconciled_aborted_session_ids identifies stale sessions that prompt rendering must skip.
# 函数用途: 从 recovery_reconciliation.groups 读取已 retired session_id，避免过期 finding 和新状态冲突。
def _reconciled_aborted_session_ids(contract: dict[str, object]) -> set[str]:
    return {
        str(session_id)
        for group in _reconciliation_groups(contract)
        for session_id in [
            *group.get("retired_session_ids", []),
            *group.get("aborted_session_ids", []),
            *group.get("already_closed_session_ids", []),
        ]
        if str(session_id)
    }


# LLM: _reconciliation_groups extracts structured cleanup records without reading stdout prose.
# 函数用途: 读取 delivery_contract.recovery_reconciliation.groups；非对象条目忽略。
def _reconciliation_groups(contract: dict[str, object]) -> list[dict[str, object]]:
    reconciliation = contract.get("recovery_reconciliation")
    groups = reconciliation.get("groups") if isinstance(reconciliation, dict) else None
    if not isinstance(groups, list):
        return []
    return [dict(item) for item in groups if isinstance(item, dict)]


# LLM: _one_runtime_finding_lines keeps each runtime finding compact and action-oriented.
# 函数用途: 根据结构化 code 渲染单条恢复提示；未知 code 只展示 code/location，不做自然语言推断。
def _one_runtime_finding_lines(
    finding: dict[str, object], artifact_items: list[dict[str, object]]
) -> list[str]:
    code = str(finding.get("code") or "").strip()
    if code == "OPEN_FILE_WRITE_SESSION":
        return _open_write_session_lines(finding)
    if code == "STAGED_JSON_INVALID":
        return _staged_json_invalid_lines(finding, artifact_items)
    if code == "STAGED_JSON_NO_ROWS":
        return _staged_json_no_rows_lines(finding, artifact_items)
    location = str(finding.get("location") or finding.get("stage_ref") or "").strip()
    return [f"- runtime_finding={code or '<unknown>'} location={location or '<none>'}"]


# LLM: _grouped_open_write_session_ids finds duplicate open write sessions by target path.
# 函数用途: 根据结构化 target_path/session_id 找到同一目标文件的重复 open session，供恢复提示跳过逐条猜测。
def _grouped_open_write_session_ids(findings: list[dict[str, object]]) -> set[str]:
    groups: dict[str, list[dict[str, object]]] = {}
    for finding in findings:
        if str(finding.get("code") or "") != "OPEN_FILE_WRITE_SESSION":
            continue
        target = _target_display(finding)
        if target:
            groups.setdefault(target, []).append(finding)
    return {
        str(item.get("session_id") or "")
        for items in groups.values()
        if len(items) > 1
        for item in items
        if str(item.get("session_id") or "")
    }


# LLM: _duplicate_open_write_session_lines renders one selected continuation path per duplicate target.
# 函数用途: 同一 target_path 有多个 open session 时，推荐已有 chunk 最多的 session，并要求关闭空/重复 session。
def _duplicate_open_write_session_lines(
    findings: list[dict[str, object]], grouped_session_ids: set[str]
) -> list[str]:
    if not grouped_session_ids:
        return []
    groups: dict[str, list[dict[str, object]]] = {}
    for finding in findings:
        session_id = str(finding.get("session_id") or "")
        if session_id in grouped_session_ids:
            groups.setdefault(_target_display(finding), []).append(finding)
    lines: list[str] = []
    for target, items in groups.items():
        chosen = max(items, key=_open_write_session_progress)
        duplicate_ids = [
            str(item.get("session_id") or "")
            for item in items
            if str(item.get("session_id") or "") != str(chosen.get("session_id") or "")
        ]
        lines.extend(
            [
                "- duplicate_open_file_write_sessions:",
                f"  - target_path={target}",
                f"  - recommended_session_id={chosen.get('session_id') or ''}",
                f"  - next_chunk_index={chosen.get('next_chunk_index', 0)}",
                f"  - preview_path={chosen.get('preview_path') or ''}",
                f"  - preview_materialized={_json_bool(chosen.get('preview_materialized'), default=False)}",
                f"  - abort_duplicate_session_ids={', '.join(duplicate_ids)}",
                "  - staged_fact_source=preview_and_chunks",
                "  - preview_materialized_after_append=true",
                "  - 只继续 recommended_session_id 并 finish；空 session 或重复 session 先 abort。",
            ]
        )
    return lines


# LLM: _open_write_session_progress ranks open write sessions by committed structured chunks.
# 函数用途: 用 received_chunks 和 next_chunk_index 选择最值得继续的 session，不读取临时文件正文。
def _open_write_session_progress(finding: dict[str, object]) -> tuple[int, int]:
    chunks = finding.get("received_chunks")
    chunk_count = len(chunks) if isinstance(chunks, list) else 0
    try:
        next_index = int(finding.get("next_chunk_index", 0))
    except (TypeError, ValueError):
        next_index = 0
    return chunk_count, next_index


# LLM: _open_write_session_lines tells the model exactly which existing session to continue or close.
# 函数用途: 渲染 open file_write_session 的 session_id、chunk 下标和目标路径，避免续跑重新猜文件状态。
def _open_write_session_lines(finding: dict[str, object]) -> list[str]:
    target_display = _target_display(finding)
    session_id = str(finding.get("session_id") or "")
    next_chunk = finding.get("next_chunk_index", 0)
    manifest = str(finding.get("manifest_path") or "")
    preview = str(finding.get("preview_path") or "")
    return [
        "- open_file_write_session:",
        f"  - session_id={session_id}",
        f"  - target_path={target_display}",
        f"  - next_chunk_index={next_chunk}",
        f"  - manifest_path={manifest}",
        f"  - preview_path={preview}",
        f"  - preview_materialized={_json_bool(finding.get('preview_materialized'), default=False)}",
        f"  - preview_char_count={finding.get('preview_char_count') or 0}",
        f"  - preview_tail={json.dumps(str(finding.get('preview_tail') or ''), ensure_ascii=False)}",
        f"  - resume_action={finding.get('resume_action') or 'append_from_next_chunk_then_finish'}",
        "  - staged_fact_source=preview_and_chunks",
        "  - preview_materialized_after_append=true",
        f"  - chunk_content_read_required={_json_bool(finding.get('chunk_content_read_required'), default=False)}",
        f"  - existing_chunks_authoritative={_json_bool(finding.get('existing_chunks_authoritative'), default=True)}",
        f"  - finish_tool_call={json.dumps(finding.get('finish_tool_call') or {}, ensure_ascii=False, sort_keys=True)}",
        f"  - abort_tool_call={json.dumps(finding.get('abort_tool_call') or {}, ensure_ascii=False, sort_keys=True)}",
        "  - 先继续 append 缺失 chunk 并 finish，或 abort 后重新按阶段产物合同写入；不要重复 begin 新 session。",
    ]


# LLM: _json_bool renders machine booleans consistently inside model-visible contract hints.
# 函数用途: 把恢复合同里的布尔字段输出为 JSON 风格 true/false，避免大小写不稳定。
def _json_bool(value: object, *, default: bool) -> str:
    return "true" if bool(default if value is None else value) else "false"


# LLM: _staged_json_no_rows_lines renders the structured data handoff after an empty source checkpoint.
# 函数用途: 当 source_data.json 无行时，提示模型先写非空 rows/sheets，再调用合同里的 builder_tool 生成 workbook。
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
        f"  - source_json_ref={staging.get('source_json_ref') or stage_ref}",
        f"  - write_shape={_checkpoint_shape_hint(stage_ref, staging)}",
        f"  - required_columns={', '.join(str(item) for item in columns)}",
        f"  - writer_tool={finding.get('writer_tool') or 'write_structured_json'}",
        f"  - builder_tool={staging.get('builder_tool') or ''}",
        f"  - workbook_ref={staging.get('workbook_ref') or ''}",
        "  - 优先用 writer_tool 写 path/rows/sheets/data，避免手写大型 JSON 字符串。",
        "  - source_json_ref 有非空 rows/sheets 后，再调用 builder_tool；不要把空 JSON 当完成。",
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
        f"  - source_json_ref={staging.get('source_json_ref') or stage_ref}",
        f"  - required_shape={_checkpoint_shape_hint(stage_ref, staging)}",
        f"  - parse_error={json.dumps(parse_error, ensure_ascii=False)}",
        f"  - writer_tool={finding.get('writer_tool') or 'write_structured_json'}",
        f"  - builder_tool={staging.get('builder_tool') or ''}",
        f"  - workbook_ref={staging.get('workbook_ref') or ''}",
        "  - 优先用 writer_tool 重写 path/rows/sheets/data，避免手动修补截断 JSON。",
        "  - 先把 source_json_ref 修成可解析的完整 JSON，再继续 builder_tool 或下一阶段产物。",
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
        for key in ("source_json_ref", "source_markdown_ref", "source_ref", "workbook_ref", "pdf_ref", "output_ref")
        if str(staging.get(key) or "").strip()
    }


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


# LLM: _target_display extracts a stable target path from structured file_write_session findings.
# 函数用途: 规范化 target_path.display/raw/resolved 字段，用于同目标 open session 去重。
def _target_display(finding: dict[str, object]) -> str:
    target = finding.get("target_path") if isinstance(finding.get("target_path"), dict) else {}
    return str(target.get("display") or target.get("raw") or target.get("resolved") or "")


__all__ = ["render_recovery_guidance_lines"]
