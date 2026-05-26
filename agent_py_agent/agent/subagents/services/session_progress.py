# LLM: Task-local subagent progress snapshots keep compact resume aware of written artifacts.
# 模块用途: 记录子代理写文件后的轻量进度，刷新 latest_continue_packet，避免压缩续跑后重复写已完成部分。

from __future__ import annotations

"""Task-local progress snapshots for subagent runner tool calls."""

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..model_task import SubAgentTask
from .compact_continue_packet import SubagentContinuePacketRequest, write_subagent_continue_packet
from .session_progress_integrity import (
    artifact_integrity_progress,
    artifact_integrity_summary,
    artifact_next_action,
)
from .session_progress_paths import progress_path

_SCHEMA_VERSION = "subagent_tool_progress.v1"
_MAX_HEADINGS = 16


# LLM: SubagentToolProgressRequest is the explicit bundle for one runner tool progress event.
# 类用途: 保存工具名、payload、输出状态和工具轮次；调用 record_subagent_tool_progress 后会写 task-local 进度文件。
@dataclass(frozen=True)
class SubagentToolProgressRequest:
    task: SubAgentTask
    tool: str
    payload: dict[str, object]
    output: str
    ok: bool
    tool_round: int
    tool_index: int
    result_envelope: dict[str, Any] = field(default_factory=dict)


# LLM: _ProgressRefs bundles the two progress files written for one agent run.
# 类用途: 保存 latest snapshot 和 append-only ledger 的路径，避免内部函数继续散传路径参数。
@dataclass(frozen=True)
class _ProgressRefs:
    latest: Path
    ledger: Path


# LLM: _CloseoutSnapshotRequest bundles internal output.json progress preservation inputs.
# 类用途: 把 output closeout 快照的多个局部变量收进一个包，避免 helper 接口继续长参数。
@dataclass(frozen=True)
class _CloseoutSnapshotRequest:
    request: SubagentToolProgressRequest
    previous: dict[str, Any]
    refs: _ProgressRefs
    path: str
    headings: list[str]
    written_paths: list[str]


# LLM: record_runtime_subagent_tool_progress bridges ToolLoopService records to task-local progress snapshots.
# 函数用途: 从运行时工具记录识别子代理写入事件，失败时静默跳过，避免进度层影响工具主流程。
def record_runtime_subagent_tool_progress(agent: object, record: object) -> dict[str, Any]:
    params = getattr(record, "params", None)
    if str(getattr(params, "context_scope", "") or "").strip().lower() != "task_local":
        return {}
    run_id = str(getattr(params, "run_id", "") or "").strip()
    if not run_id or not hasattr(agent, "subagents"):
        return {}
    try:
        task = agent.subagents.load(run_id)
    except (FileNotFoundError, json.JSONDecodeError, TypeError, AttributeError):
        return {}
    result = getattr(record, "result", None)
    payload = getattr(record, "payload", {})
    progress = record_subagent_tool_progress(
        SubagentToolProgressRequest(
            task=task,
            tool=str(getattr(result, "tool", "") or ""),
            payload=payload if isinstance(payload, dict) else {},
            output=str(getattr(result, "output", "") or ""),
            result_envelope=(
                dict(getattr(result, "result_envelope", {}) or {})
                if isinstance(getattr(result, "result_envelope", {}), dict)
                else {}
            ),
            ok=bool(getattr(result, "ok", False)),
            tool_round=int(getattr(record, "tool_rounds", 0) or 0),
            tool_index=int(getattr(record, "idx", 0) or 0),
        )
    )
    _persist_runtime_status(agent, task, result, progress)
    return progress


# LLM: record_subagent_tool_progress writes a compact, cumulative progress snapshot for write-like tools.
# 函数用途: 子代理成功写文件后记录路径、标题和摘要，并刷新 task-local continue packet。
def record_subagent_tool_progress(request: SubagentToolProgressRequest) -> dict[str, Any]:
    if not _should_record(request):
        return {}
    progress_dir = _progress_dir(request.task)
    if not progress_dir:
        return {}
    progress_dir.mkdir(parents=True, exist_ok=True)
    refs = _ProgressRefs(progress_dir / "latest_tool_progress.json", progress_dir / "tool_progress.jsonl")
    previous = _read_json_object(refs.latest)
    snapshot = _snapshot_payload(request, previous, refs)
    refs.latest.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8")
    with refs.ledger.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(snapshot, ensure_ascii=False, sort_keys=True) + "\n")
    _refresh_continue_packet(request.task, snapshot)
    return snapshot


# LLM: _should_record limits progress writes to successful filesystem mutations.
# 函数用途: 只记录成功且能解析出产物路径的工具；支持未来工具通过 artifact_ref/path 自说明。
def _should_record(request: SubagentToolProgressRequest) -> bool:
    return bool(request.ok and isinstance(request.payload, dict) and progress_path(request))


# LLM: _persist_runtime_status updates the lightweight agent tree view after each runner tool call.
# 函数用途: 只回写观测字段和最近进展，不调度、不验收、不改变任务完成状态。
def _persist_runtime_status(agent: object, task: SubAgentTask, result: object, progress: dict[str, Any]) -> None:
    now = time.time()
    tool = str(getattr(result, "tool", "") or "")
    ok = bool(getattr(result, "ok", False))
    task.current_tool = tool
    task.heartbeat_at = now
    task.updated_at = now
    if ok and tool:
        task.last_progress_at = now
        task.last_progress_summary = _progress_summary(tool, progress)
        if progress:
            task.latest_summary = str(progress.get("summary") or task.latest_summary or "")
            task.current_step = str(progress.get("next_action") or task.current_step or "")
    try:
        agent.subagents.save(task)
    except Exception:
        return


# LLM: _progress_summary keeps status rows concise and avoids copying full tool output.
# 函数用途: 为只读状态树生成一句最近进展；写入类工具优先用已生成的 progress 摘要。
def _progress_summary(tool: str, progress: dict[str, Any]) -> str:
    summary = str(progress.get("summary") or "").strip() if isinstance(progress, dict) else ""
    if summary:
        return summary
    return f"最近成功调用工具: {tool}"


# LLM: _progress_dir derives the stable progress directory inside the agent run workspace.
# 函数用途: 将进度快照放到 agents/<run_id>/progress 下；旧任务缺 workspace 时安全跳过。
def _progress_dir(task: SubAgentTask) -> Path | None:
    workspace = str(getattr(task, "agent_run_workspace_dir", "") or "").strip()
    return Path(workspace) / "progress" if workspace else None


# LLM: _snapshot_payload merges new write facts with prior headings so each compact has cumulative progress.
# 函数用途: 生成 latest_tool_progress.json 内容，包括已写路径、标题、摘要和继续建议。
def _snapshot_payload(
    request: SubagentToolProgressRequest,
    previous: dict[str, Any],
    refs: _ProgressRefs,
) -> dict[str, Any]:
    path = progress_path(request)
    headings = _merge_unique(_string_list(previous.get("headings")) + _headings_from_payload(request.payload))
    written_paths = _merge_unique(_string_list(previous.get("written_paths")) + ([path] if path else []))
    if _is_internal_output_path(request.task, path) and previous:
        return _output_closeout_snapshot(
            _CloseoutSnapshotRequest(request, previous, refs, path, headings, written_paths)
        )
    integrity = artifact_integrity_progress(path)
    summary = _summary(request.tool, path, headings, integrity)
    next_action = artifact_next_action(integrity)
    return {
        "schema_version": _SCHEMA_VERSION,
        "kind": "subagent_tool_progress",
        "run_id": request.task.id,
        "root_id": request.task.root_id or request.task.id,
        "tool": request.tool,
        "tool_round": request.tool_round,
        "tool_index": request.tool_index,
        "latest_written_path": path,
        "written_paths": written_paths,
        "headings": headings[:_MAX_HEADINGS],
        "summary": summary,
        "next_action": next_action,
        "artifact_integrity": integrity,
        "latest_tool_progress_ref": str(refs.latest),
        "tool_progress_ledger_ref": str(refs.ledger),
        "output_preview": _clip(request.output, 300),
        "reserved": {},
    }


# LLM: _output_closeout_snapshot preserves product progress when the runner writes internal output.json.
# 函数用途: 子代理写内部收口文件时，不让 output.json 覆盖真实产物的最新自检状态和修复建议。
def _output_closeout_snapshot(closeout: _CloseoutSnapshotRequest) -> dict[str, Any]:
    request = closeout.request
    snapshot = dict(closeout.previous)
    snapshot.update({
        "tool": request.tool,
        "tool_round": request.tool_round,
        "tool_index": request.tool_index,
        "written_paths": closeout.written_paths,
        "headings": closeout.headings[:_MAX_HEADINGS],
        "latest_tool_progress_ref": str(closeout.refs.latest),
        "tool_progress_ledger_ref": str(closeout.refs.ledger),
        "closeout_written_path": closeout.path,
        "closeout_output_preview": _clip(request.output, 300),
        "reserved": dict(closeout.previous.get("reserved") or {}),
    })
    return snapshot


# LLM: _is_internal_output_path recognizes the runner closeout file, not a user product artifact.
# 函数用途: 判断当前写入是否是 task.output_json；只有内部收口文件才保留上一次产品进度。
def _is_internal_output_path(task: SubAgentTask, path: str) -> bool:
    if not path:
        return False
    try:
        return Path(path).resolve() == Path(str(getattr(task, "output_json", "") or "")).resolve()
    except OSError:
        return False


# LLM: _refresh_continue_packet promotes progress snapshot facts into the task-local continue packet.
# 函数用途: 更新 packet 的 latest_summary/work_progress/recommended_read_paths，让恢复优先看进度快照。
def _refresh_continue_packet(task: SubAgentTask, snapshot: dict[str, Any]) -> None:
    task.latest_summary = str(snapshot.get("summary") or task.latest_summary or "")
    task.current_step = str(snapshot.get("next_action") or task.current_step or "")
    write_subagent_continue_packet(
        SubagentContinuePacketRequest(
            task,
            {
                "summary": task.latest_summary,
                "next_action": task.current_step,
                "work_progress": snapshot,
            },
        )
    )


# LLM: _headings_from_payload extracts markdown headings from model-written content without reading files.
# 函数用途: 从 write_file content 参数里提取标题，形成续跑时可对照的已完成章节清单。
def _headings_from_payload(payload: dict[str, object]) -> list[str]:
    content = str(payload.get("content") or "")
    headings: list[str] = []
    for raw in content.splitlines():
        line = raw.strip()
        if not line.startswith("#"):
            continue
        heading = line.lstrip("#").strip()
        if heading:
            headings.append(heading)
    return headings[:_MAX_HEADINGS]


# LLM: _summary renders one concise Chinese progress line for packet and board display.
# 函数用途: 生成“最近写了什么、有哪些标题”的短摘要，帮助模型续跑时先校准进度。
def _summary(tool: str, path: str, headings: list[str], integrity: dict[str, Any]) -> str:
    name = Path(path).name if path else "未命名文件"
    integrity_summary = artifact_integrity_summary(integrity)
    if headings:
        base = f"最近 {tool} {name}；已记录标题：{'；'.join(headings[:6])}"
    else:
        base = f"最近 {tool} {name}；尚未识别到 Markdown 标题"
    return f"{base}；{integrity_summary}" if integrity_summary else base


# LLM: _read_json_object tolerates missing progress files during the first write.
# 函数用途: 读取 previous latest_tool_progress；不存在或损坏时返回空对象。
def _read_json_object(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


# LLM: _string_list normalizes optional JSON list fields before merging.
# 函数用途: 将已有 headings/written_paths 统一成字符串列表。
def _string_list(value: object) -> list[str]:
    if isinstance(value, list):
        return [str(item) for item in value if str(item or "").strip()]
    return []


# LLM: _merge_unique keeps first-seen order so repeated append rounds do not duplicate progress facts.
# 函数用途: 按顺序去重标题和文件路径。
def _merge_unique(values: list[str]) -> list[str]:
    return list(dict.fromkeys(item for item in values if str(item or "").strip()))


# LLM: _clip keeps tool output preview small in progress snapshots.
# 函数用途: 截断工具输出摘要，避免进度快照携带大正文。
def _clip(text: str, limit: int) -> str:
    value = str(text or "")
    return value if len(value) <= limit else value[:limit].rstrip() + "...<truncated>"


__all__ = [
    "SubagentToolProgressRequest",
    "record_runtime_subagent_tool_progress",
    "record_subagent_tool_progress",
]
