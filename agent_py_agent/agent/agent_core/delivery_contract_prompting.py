# LLM: Delivery contract prompting renders structured contracts into model-visible execution hints.
# 模块用途: 把 RunParams.delivery_contract 转成人类可读执行提示；系统事实仍以结构化合同字段为准。

from __future__ import annotations

import json


# LLM: render_delivery_contract_section keeps delivery instructions derived from JSON fields.
# 函数用途: 渲染交付合同提示段，帮助模型执行路径/格式/资源约束，不让系统从提示反向取事实。
def render_delivery_contract_section(contract: dict[str, object]) -> str:
    return "\n".join(
        [
            "[tool-system delivery-contract]",
            json.dumps(contract, ensure_ascii=False, sort_keys=True),
            *_bootstrap_guidance_lines(contract),
            *_artifact_guidance_lines(contract),
            *_recovery_guidance_lines(contract),
        ]
    )


# LLM: _bootstrap_guidance_lines turns structured startup targets into concise first-round execution hints.
# 函数用途: 根据 bootstrap_contract 渲染通用开工顺序，避免模型前几轮一直只读检查目录。
def _bootstrap_guidance_lines(contract: dict[str, object]) -> list[str]:
    bootstrap = contract.get("bootstrap_contract")
    if not isinstance(bootstrap, dict):
        return []
    targets = _bootstrap_targets(bootstrap.get("materialization_targets"))
    actions = _bootstrap_actions(bootstrap.get("startup_actions"))
    if not targets and not actions:
        return []
    lines = ["开工顺序："]
    if targets:
        lines.append("- 前两轮至少让下面这些结构化目标中的一个真实出现，不要连续两轮只做目录查看。")
        lines.extend(f"  - {target}" for target in targets[:6])
        lines.append("- 阶段目标允许先写最小有效骨架：例如空 JSON 数组、带标题的 Markdown 草稿、最小可运行脚本或基础 HTML 壳子，后续再补全内容。")
    if actions:
        lines.extend(_startup_action_lines(actions))
    lines.append("- 如果暂时不确定具体工具，可以先 list_tools 一次，但紧接着就开始物化目标路径。")
    return lines


# LLM: _artifact_guidance_lines turns artifact machine fields into concise model guidance.
# 函数用途: 根据 artifacts.validation_contract 生成执行提示，避免模型忽略单文件和完整文档要求。
def _artifact_guidance_lines(contract: dict[str, object]) -> list[str]:
    lines = ["执行要求："]
    for artifact in _artifact_items(contract):
        lines.extend(_one_artifact_lines(artifact))
    lines.append("- 如果 required artifact 还不存在，前两轮优先对该 artifact 的目标路径动手：创建目录、开始写入或补齐阶段产物。")
    lines.append("- 不要把前两轮都花在只读检查上；先让 required artifact 或阶段产物出现，再继续精修。")
    lines.append("如果内容较长，使用 file_write_session 分块写入，并在最终答复前 finish。")
    return lines


# LLM: _bootstrap_targets formats materialization targets for model-visible startup hints.
# 函数用途: 把 bootstrap_contract.materialization_targets 里的结构化路径压缩成简短提示行。
def _bootstrap_targets(items: object) -> list[str]:
    if not isinstance(items, list):
        return []
    lines: list[str] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        relative = str(item.get("workspace_relative_path") or "").strip()
        target_type = str(item.get("target_type") or "").strip()
        if relative:
            lines.append(f"{target_type or 'target'}: {relative}")
    return lines


# LLM: _bootstrap_actions normalizes startup action objects from the generic delivery contract.
# 函数用途: 读取 bootstrap_contract.startup_actions，过滤非对象项。
def _bootstrap_actions(items: object) -> list[dict[str, object]]:
    if not isinstance(items, list):
        return []
    return [dict(item) for item in items if isinstance(item, dict)]


# LLM: _startup_action_lines keeps startup guidance generic and based on structured action codes only.
# 函数用途: 渲染 materialize_target / invoke_builder_tool 等开工动作，不靠任务文案推断。
def _startup_action_lines(actions: list[dict[str, object]]) -> list[str]:
    lines: list[str] = []
    for action in sorted(actions, key=lambda item: int(item.get("priority", 0))):
        code = str(action.get("action") or "").strip()
        if code == "materialize_target":
            lines.append("- 先创建目录并开始写入第一个目标路径，再继续补齐其余内容。")
            continue
        if code == "materialize_checkpoint":
            checkpoint_ref = str(action.get("checkpoint_ref") or "").strip()
            if checkpoint_ref:
                lines.append(f"- 先真实写出 checkpoint: {checkpoint_ref}")
                lines.append(f"- 如果资料还没收全，先给 {checkpoint_ref} 写最小有效骨架，再继续抓取/整理。")
            continue
        if code == "invoke_builder_tool":
            builder = str(action.get("builder_tool") or "").strip()
            source_ref = str(action.get("source_ref") or "").strip()
            output_ref = str(action.get("output_ref") or "").strip()
            if builder and source_ref and output_ref:
                lines.append(f"- 阶段数据就绪后，优先调用 {builder}: source_json_path={source_ref}, path={output_ref}")
            elif builder:
                lines.append(f"- 阶段数据就绪后，优先调用 {builder} 生成后续产物。")
    return lines


# LLM: _one_artifact_lines summarizes one artifact contract without changing validation behavior.
# 函数用途: 输出单个产物的路径、类型和关键质量要求，供模型更稳地生成可验收文件。
def _one_artifact_lines(artifact: dict[str, object]) -> list[str]:
    contract = artifact.get("validation_contract") if isinstance(artifact.get("validation_contract"), dict) else {}
    requirements = contract.get("quality_requirements") if isinstance(contract.get("quality_requirements"), dict) else {}
    lines = [f"- 写入产物路径: {artifact.get('preferred_path') or artifact.get('path') or '<missing>'}"]
    if artifact.get("kind"):
        lines.append(f"- 产物类型: {artifact.get('kind')}")
    if requirements.get("complete_html_document"):
        lines.append("- HTML 必须包含完整 doctype/html/head/body 闭合结构。")
    if requirements.get("single_file_no_external_assets"):
        lines.append("- 单文件产物不得引用 http/https 外部 CSS、字体、图片或脚本。")
    if requirements.get("min_size_bytes"):
        lines.append(f"- 产物体量至少 {requirements.get('min_size_bytes')} bytes，但不要无意义膨胀。")
    lines.extend(_staging_lines(contract))
    return lines


# LLM: _staging_lines renders structured checkpoint refs for long-running deliverables.
# 函数用途: 把 validation_contract.staging_contract 展示给模型，帮助按数据/脚本/最终产物分段执行。
def _staging_lines(contract: dict[str, object]) -> list[str]:
    staging = contract.get("staging_contract")
    if not isinstance(staging, dict):
        return []
    refs = staging.get("checkpoint_refs")
    if not isinstance(refs, list):
        return []
    lines = ["- 阶段产物:"]
    lines.extend(f"  - {ref}" for ref in refs if str(ref).strip())
    builder_tool = str(staging.get("builder_tool") or "").strip()
    if builder_tool:
        lines.append(f"- 阶段构建工具: {builder_tool}")
    source_ref = str(staging.get("source_json_ref") or "").strip()
    workbook_ref = str(staging.get("workbook_ref") or "").strip()
    if source_ref and workbook_ref and builder_tool:
        lines.append(f"- 生成表格时优先调用 {builder_tool}: source_json_path={source_ref}, path={workbook_ref}")
        lines.append("- source_json_path 可以先写最小有效 JSON 骨架，再逐步补齐行数据；不要等资料全齐才第一次落盘。")
    return lines


# LLM: _artifact_items normalizes contract artifacts without reading prompt text.
# 函数用途: 从结构化合同读取 artifact 列表；非对象条目会被忽略。
def _artifact_items(contract: dict[str, object]) -> list[dict[str, object]]:
    items = contract.get("artifacts")
    return [dict(item) for item in items] if isinstance(items, list) and all(isinstance(item, dict) for item in items) else []


# LLM: _recovery_guidance_lines renders machine recovery findings without parsing prior stdout prose.
# 函数用途: 将 recovery.acceptance.runtime_findings 里的结构化恢复事实展示给模型，优先处理 open file_write_session。
def _recovery_guidance_lines(contract: dict[str, object]) -> list[str]:
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
        lines.extend(_one_runtime_finding_lines(finding, contract))
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
def _one_runtime_finding_lines(finding: dict[str, object], contract: dict[str, object]) -> list[str]:
    code = str(finding.get("code") or "").strip()
    if code == "OPEN_FILE_WRITE_SESSION":
        return _open_write_session_lines(finding)
    if code == "STAGED_JSON_INVALID":
        return _staged_json_invalid_lines(finding, contract)
    if code == "STAGED_JSON_NO_ROWS":
        return _staged_json_no_rows_lines(finding, contract)
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
        "  - 先继续 append 缺失 chunk 并 finish，或 abort 后重新按阶段产物合同写入；不要重复 begin 新 session。",
    ]


# LLM: _json_bool renders machine booleans consistently inside model-visible contract hints.
# 函数用途: 把恢复合同里的布尔字段输出为 JSON 风格 true/false，避免大小写不稳定。
def _json_bool(value: object, *, default: bool) -> str:
    return "true" if bool(default if value is None else value) else "false"


# LLM: _staged_json_no_rows_lines renders the structured data handoff after an empty source checkpoint.
# 函数用途: 当 source_data.json 无行时，提示模型先写非空 rows/sheets，再调用合同里的 builder_tool 生成 workbook。
def _staged_json_no_rows_lines(
    finding: dict[str, object], contract: dict[str, object]
) -> list[str]:
    stage_ref = str(finding.get("stage_ref") or finding.get("location") or "")
    artifact = _artifact_for_stage_ref(contract, stage_ref)
    validation = artifact.get("validation_contract") if isinstance(artifact.get("validation_contract"), dict) else {}
    staging = validation.get("staging_contract") if isinstance(validation.get("staging_contract"), dict) else {}
    columns = validation.get("required_columns") if isinstance(validation.get("required_columns"), list) else []
    return [
        "- staged_json_no_rows:",
        f"  - source_json_ref={staging.get('source_json_ref') or stage_ref}",
        f"  - write_shape={_checkpoint_shape_hint(stage_ref, staging)}",
        f"  - required_columns={', '.join(str(item) for item in columns)}",
        f"  - builder_tool={staging.get('builder_tool') or ''}",
        f"  - workbook_ref={staging.get('workbook_ref') or ''}",
        "  - source_json_ref 有非空 rows/sheets 后，再调用 builder_tool；不要把空 JSON 当完成。",
    ]


# LLM: _staged_json_invalid_lines renders checkpoint-repair guidance from structured refs only.
# 函数用途: 当阶段 JSON 语法坏掉或被截断时，提示模型先修复结构化 JSON，再继续 builder/下一阶段产物。
def _staged_json_invalid_lines(
    finding: dict[str, object], contract: dict[str, object]
) -> list[str]:
    stage_ref = str(finding.get("stage_ref") or finding.get("location") or "")
    artifact = _artifact_for_stage_ref(contract, stage_ref)
    validation = artifact.get("validation_contract") if isinstance(artifact.get("validation_contract"), dict) else {}
    staging = validation.get("staging_contract") if isinstance(validation.get("staging_contract"), dict) else {}
    parse_error = str(finding.get("parse_error") or "")
    return [
        "- staged_json_invalid:",
        f"  - source_json_ref={staging.get('source_json_ref') or stage_ref}",
        f"  - required_shape={_checkpoint_shape_hint(stage_ref, staging)}",
        f"  - parse_error={json.dumps(parse_error, ensure_ascii=False)}",
        f"  - builder_tool={staging.get('builder_tool') or ''}",
        f"  - workbook_ref={staging.get('workbook_ref') or ''}",
        "  - 先把 source_json_ref 修成可解析的完整 JSON，再继续 builder_tool 或下一阶段产物。",
    ]


# LLM: _artifact_for_stage_ref finds the artifact contract that owns a staged checkpoint ref.
# 函数用途: 用 staging_contract.checkpoint_refs/source_json_ref 匹配 artifact，不根据提示词或 stdout 推断。
def _artifact_for_stage_ref(contract: dict[str, object], stage_ref: str) -> dict[str, object]:
    for artifact in _artifact_items(contract):
        validation = artifact.get("validation_contract") if isinstance(artifact.get("validation_contract"), dict) else {}
        staging = validation.get("staging_contract") if isinstance(validation.get("staging_contract"), dict) else {}
        refs = staging.get("checkpoint_refs") if isinstance(staging.get("checkpoint_refs"), list) else []
        if stage_ref in refs or stage_ref == str(staging.get("source_json_ref") or ""):
            return artifact
    return {}


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


__all__ = ["render_delivery_contract_section"]
