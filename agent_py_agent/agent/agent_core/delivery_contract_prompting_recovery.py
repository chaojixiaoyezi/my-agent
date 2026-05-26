# LLM: Delivery recovery prompting keeps continuation hints derived from structured recovery findings only.
# 模块用途: 渲染 delivery_contract.recovery / recovery_reconciliation 里的机器恢复事实，不让系统回读 stdout 或自然语言摘要。

from __future__ import annotations

import json

from .delivery_contract_prompting_staged import (
    _staged_json_invalid_lines,
    _staged_json_no_rows_lines,
)


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


from .delivery_contract_prompting_recovery_bool import _json_bool


# LLM: _target_display extracts a stable target path from structured file_write_session findings.
# 函数用途: 规范化 target_path.display/raw/resolved 字段，用于同目标 open session 去重。
def _target_display(finding: dict[str, object]) -> str:
    target = finding.get("target_path") if isinstance(finding.get("target_path"), dict) else {}
    return str(target.get("display") or target.get("raw") or target.get("resolved") or "")


__all__ = ["render_recovery_guidance_lines"]
