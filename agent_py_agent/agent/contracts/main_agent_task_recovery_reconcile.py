# LLM: Real-task recovery reconciliation fixes machine state before asking the model to continue.
# 模块用途: 根据 recovery_packet 的结构化 findings 整理续跑状态，减少模型复制长 session_id 的失败面。

from __future__ import annotations

import json
import time
from pathlib import Path

from ..tooling.file_write_session_inspection import open_file_write_sessions
from .main_agent_task_recovery_resume import recovery_packet_payload


# LLM: reconcile_recovery_open_write_sessions aborts duplicate open write sessions for the same target.
# 函数用途: 续跑真实任务前，保留同目标中进度最多的 file_write_session，并把空/重复 session 标成 aborted。
def reconcile_recovery_open_write_sessions(
    packet_path: Path | None,
    *,
    workspace: Path,
) -> dict[str, object]:
    payload = recovery_packet_payload(packet_path)
    task_workspace = _task_workspace(payload, workspace)
    findings = _dedupe_findings([*_workspace_findings(task_workspace), *_runtime_findings(payload)])
    groups = _duplicate_groups(findings)
    summaries = [
        _reconcile_group(target, items, task_workspace)
        for target, items in groups.items()
        if len(items) > 1
    ]
    summaries = [item for item in summaries if item.get("retired_session_ids")]
    if not summaries:
        return {}
    return {"schema_version": "task-recovery-reconcile.v1", "groups": summaries}


# LLM: _runtime_findings extracts only structured acceptance findings from the recovery packet.
# 函数用途: 读取 acceptance.runtime_findings；不读取 stdout/stderr 或用户提示词。
def _runtime_findings(payload: dict[str, object]) -> list[dict[str, object]]:
    acceptance = payload.get("acceptance")
    findings = acceptance.get("runtime_findings") if isinstance(acceptance, dict) else None
    return [dict(item) for item in findings if isinstance(item, dict)] if isinstance(findings, list) else []


# LLM: _workspace_findings adds current open write sessions that may have appeared after the packet.
# 函数用途: 扫描当前任务工作区 open session manifest，补齐恢复包之后新增的结构化运行事实。
def _workspace_findings(task_workspace: Path) -> list[dict[str, object]]:
    return [_open_session_finding(item) for item in open_file_write_sessions(task_workspace, limit=50)]


# LLM: _open_session_finding mirrors acceptance runtime finding shape for current workspace scans.
# 函数用途: 将 open_file_write_sessions 的摘要转换成恢复去重可用的 OPEN_FILE_WRITE_SESSION finding。
def _open_session_finding(session: dict[str, object]) -> dict[str, object]:
    return {
        "code": "OPEN_FILE_WRITE_SESSION",
        "severity": "hard",
        "session_id": str(session.get("session_id") or ""),
        "target_path": session.get("target_path") or {},
        "manifest_path": str(session.get("manifest_path") or ""),
        "preview_path": str(session.get("preview_path") or ""),
        "preview_materialized": bool(session.get("preview_materialized")),
        "received_chunks": list(session.get("received_chunks") or []),
        "next_chunk_index": int(session.get("next_chunk_index") or 0),
        "continue_tool_call": session.get("continue_tool_call") or {},
        "finish_tool_call": session.get("finish_tool_call") or {},
        "abort_tool_call": session.get("abort_tool_call") or {},
        "abort_requires_discard_chunks": bool(session.get("abort_requires_discard_chunks")),
    }


# LLM: _dedupe_findings avoids double-counting the same manifest when packet and workspace agree.
# 函数用途: 用 session_id/manifest_path 去重恢复 findings，保留第一条结构化事实。
def _dedupe_findings(findings: list[dict[str, object]]) -> list[dict[str, object]]:
    seen: set[tuple[str, str]] = set()
    deduped: list[dict[str, object]] = []
    for finding in findings:
        key = (str(finding.get("session_id") or ""), str(finding.get("manifest_path") or ""))
        if key in seen:
            continue
        seen.add(key)
        deduped.append(finding)
    return deduped


# LLM: _task_workspace resolves the task workspace ref from recovery packet refs.
# 函数用途: 根据 refs.task_workspace_ref 找到任务工作区；缺失时回退到 real-e2e 根目录。
def _task_workspace(payload: dict[str, object], workspace: Path) -> Path:
    refs = payload.get("refs") if isinstance(payload.get("refs"), dict) else {}
    raw = str(refs.get("task_workspace_ref") or "")
    candidate = Path(raw)
    return candidate if candidate.is_absolute() else (workspace / candidate).resolve()


# LLM: _duplicate_groups groups open write-session findings by target path.
# 函数用途: 用 target_path.display/raw/resolved 识别同一目标文件的重复 session。
def _duplicate_groups(findings: list[dict[str, object]]) -> dict[str, list[dict[str, object]]]:
    groups: dict[str, list[dict[str, object]]] = {}
    for finding in findings:
        if str(finding.get("code") or "") != "OPEN_FILE_WRITE_SESSION":
            continue
        target = _target_path(finding)
        if target:
            groups.setdefault(target, []).append(finding)
    return groups


# LLM: _reconcile_group aborts every duplicate except the session with most chunk progress.
# 函数用途: 同目标重复 session 只保留一个可继续对象，避免下一轮模型手打多个长 ID。
def _reconcile_group(
    target: str,
    findings: list[dict[str, object]],
    task_workspace: Path,
) -> dict[str, object]:
    chosen = max(findings, key=_progress)
    retired = [
        _retire_manifest(item, task_workspace)
        for item in findings
        if str(item.get("session_id") or "") != str(chosen.get("session_id") or "")
    ]
    aborted_session_ids = [
        str(item["session_id"]) for item in retired if item.get("action") == "aborted"
    ]
    already_closed_session_ids = [
        str(item["session_id"]) for item in retired if item.get("action") == "already_closed"
    ]
    return {
        "target_path": target,
        "kept_session_id": str(chosen.get("session_id") or ""),
        "kept_received_chunks": _received_chunks(chosen),
        "kept_next_chunk_index": _next_chunk_index(chosen),
        "retired_session_ids": [str(item["session_id"]) for item in retired if item.get("session_id")],
        "aborted_session_ids": aborted_session_ids,
        "already_closed_session_ids": already_closed_session_ids,
    }


# LLM: _retire_manifest records every non-kept duplicate so stale packets cannot revive it.
# 函数用途: open manifest 会被标为 aborted；已经 closed/aborted 的 manifest 会作为 retired 结构化返回。
def _retire_manifest(finding: dict[str, object], task_workspace: Path) -> dict[str, str]:
    path = _manifest_path(finding, task_workspace)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"session_id": str(finding.get("session_id") or ""), "action": "missing_or_invalid"}
    session_id = str(payload.get("session_id") or finding.get("session_id") or "")
    if payload.get("status") != "open":
        return {
            "session_id": session_id,
            "action": "already_closed",
            "status": str(payload.get("status") or ""),
        }
    payload["status"] = "aborted"
    payload["reconciled_reason"] = "duplicate_open_file_write_session"
    payload["reconciled_at_unix"] = round(time.time(), 3)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    return {"session_id": session_id, "action": "aborted", "status": "aborted"}


# LLM: _manifest_path resolves a finding manifest path under the task workspace.
# 函数用途: 支持绝对和相对 manifest_path，并拒绝越出 task workspace 的路径。
def _manifest_path(finding: dict[str, object], task_workspace: Path) -> Path:
    raw = str(finding.get("manifest_path") or "")
    candidate = Path(raw)
    path = candidate if candidate.is_absolute() else (task_workspace / candidate).resolve()
    try:
        path.resolve().relative_to(task_workspace.resolve())
    except ValueError as exc:
        raise ValueError("manifest_path outside task workspace") from exc
    return path


# LLM: _progress ranks sessions by received chunks and next chunk index.
# 函数用途: 选择最值得继续的 session；只读结构化 chunks 字段，不读 chunk 内容。
def _progress(finding: dict[str, object]) -> tuple[int, int]:
    chunks = _received_chunks(finding)
    return len(chunks), _next_chunk_index(finding)


# LLM: _received_chunks normalizes chunk progress from structured findings.
# 函数用途: 返回 received_chunks 的整型列表，供排序和 summary 字段复用。
def _received_chunks(finding: dict[str, object]) -> list[int]:
    chunks = finding.get("received_chunks")
    if not isinstance(chunks, list):
        return []
    return [int(item) for item in chunks]


# LLM: _next_chunk_index normalizes the next append index from structured findings.
# 函数用途: 读取 next_chunk_index，坏值回退 0，不解析任何文本。
def _next_chunk_index(finding: dict[str, object]) -> int:
    try:
        return int(finding.get("next_chunk_index", 0))
    except (TypeError, ValueError):
        return 0


# LLM: _target_path extracts a stable target identifier from structured finding fields.
# 函数用途: 规范化 target_path 字段，用于重复 session 分组。
def _target_path(finding: dict[str, object]) -> str:
    target = finding.get("target_path") if isinstance(finding.get("target_path"), dict) else {}
    return str(target.get("display") or target.get("raw") or target.get("resolved") or "")


__all__ = ["reconcile_recovery_open_write_sessions"]
