# LLM: Subagent compact continuation prompt text must stay task-local and refs-first.
# 模块用途: 从子代理 run workspace 读取很小的恢复线索，生成 runner prompt 的接续段，不触碰主代理长期记忆。

from __future__ import annotations

"""Task-local compact continuation prompt section for subagent runners."""

import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..subagent import SubAgentExecutionContext

_DEFAULT_SNIPPET_CHARS = 1200
_MAX_PACKET_JSON_CHARS = 200_000
_STALE_PACKET_SECONDS = 7 * 24 * 60 * 60
_REF_KEYS = (
    "agent_run_task",
    "agent_run_checkpoint",
    "agent_run_summary",
    "agent_run_latest_session_compaction_metadata",
    "agent_run_latest_session_compaction_summary",
    "agent_run_final_report",
    "agent_run_findings",
    "agent_run_timeline",
)


# LLM: SubagentCompactContinuationRequest keeps prompt continuation inputs explicit and bundle-shaped.
# 类用途: 包装子代理执行上下文和读取字符上限，避免 prompt 构建函数继续增加散参数。
@dataclass(frozen=True)
class SubagentCompactContinuationRequest:
    context: SubAgentExecutionContext
    max_chars: int = _DEFAULT_SNIPPET_CHARS


# LLM: build_subagent_compact_continuation_section renders only local refs and bounded snippets.
# 函数用途: 为子代理 runner prompt 生成任务本地 compact 接续段；不读取 SOUL/USER/AGENTS 或主 memory。
def build_subagent_compact_continuation_section(request: SubagentCompactContinuationRequest) -> str:
    refs = _workspace_refs(request.context)
    packet = _latest_continue_packet_path(refs)
    existing_refs = _existing_refs(refs)
    if not existing_refs and not packet:
        return ""
    lines = [
        "## Task-Local Compact Continuation",
        "",
        "- memory_scope: task_local",
        "- writes_main_memory: false",
        "- automatic_tool_execution: none",
        "- 只从下面的子代理任务目录接续；不要读取或写入主代理长期 memory。",
        "",
    ]
    lines.extend(_preflight_lines(request.context.context_bundle, request.max_chars))
    lines.extend(_packet_lines(packet, request.max_chars))
    lines.extend(_session_compact_lines(existing_refs, request.max_chars))
    lines.extend(_ref_lines(existing_refs))
    lines.extend(_snippet_lines(existing_refs, request.max_chars))
    return "\n".join(lines).rstrip()


# LLM: _workspace_refs tolerates older context bundles and derives missing run-workspace refs.
# 函数用途: 从 context_bundle.workspace_refs 中取路径；缺少细分字段时从 agent_run_workspace 补出标准文件名。
def _workspace_refs(context: SubAgentExecutionContext) -> dict[str, str]:
    bundle = context.context_bundle if isinstance(context.context_bundle, dict) else {}
    refs = bundle.get("workspace_refs") if isinstance(bundle.get("workspace_refs"), dict) else {}
    values = {str(key): str(value) for key, value in refs.items() if str(value or "").strip()}
    run_workspace = values.get("agent_run_workspace", "")
    if run_workspace:
        base = Path(run_workspace)
        values.setdefault("agent_run_task", str(base / "task.md"))
        values.setdefault("agent_run_checkpoint", str(base / "checkpoint.json"))
        values.setdefault("agent_run_summary", str(base / "summary.md"))
        values.setdefault("agent_run_final_report", str(base / "final_report.md"))
        values.setdefault("agent_run_findings", str(base / "findings.jsonl"))
        values.setdefault("agent_run_timeline", str(base / "timeline.jsonl"))
        values.setdefault("agent_run_compactions", str(base / "compactions"))
    compactions = values.get("agent_run_compactions", "")
    if compactions:
        session = Path(compactions) / "session"
        values.setdefault("agent_run_latest_continue_packet", str(session / "latest_continue_packet.json"))
        values.setdefault("agent_run_latest_session_compaction_metadata", str(session / "latest_metadata.json"))
        values.setdefault("agent_run_latest_session_compaction_summary", str(session / "latest_summary.md"))
        values.setdefault("agent_run_latest_compaction_metadata", str(Path(compactions) / "latest_metadata.json"))
        values.setdefault("agent_run_latest_compaction_summary", str(Path(compactions) / "latest_summary.md"))
    return values


# LLM: _latest_continue_packet_path returns a real file only, so stale reserved refs do not thicken prompts.
# 函数用途: 找到已经存在的 latest_continue_packet.json；不存在时返回 None，避免暗示可恢复包已就绪。
def _latest_continue_packet_path(refs: dict[str, str]) -> Path | None:
    value = refs.get("agent_run_latest_continue_packet", "")
    if not value and refs.get("agent_run_compactions"):
        value = str(Path(refs["agent_run_compactions"]) / "session" / "latest_continue_packet.json")
    path = Path(value) if value else None
    return path if path and path.exists() else None


# LLM: _existing_refs filters prompt refs to files and dirs that are currently usable.
# 函数用途: 只展示当前存在的任务本地恢复文件，避免 runner 追不存在路径。
def _existing_refs(refs: dict[str, str]) -> dict[str, str]:
    result: dict[str, str] = {}
    for key in _REF_KEYS:
        value = refs.get(key, "")
        if value and Path(value).exists():
            result[key] = value
    return result


# LLM: _packet_lines summarizes continue packet metadata without dumping the whole JSON body.
# 函数用途: 渲染 latest_continue_packet 的 ready、mode、next_action 和 recommended paths。
def _packet_lines(path: Path | None, max_chars: int) -> list[str]:
    if not path:
        return []
    payload, status = _read_json_with_status(path)
    lines = ["### Continue Packet", "", f"- latest_continue_packet: {path}"]
    if status != "ok":
        lines.extend([
            f"- packet_status: {status}",
            "- fallback_to: checkpoint/summary/task-local refs",
        ])
        return lines + [""]
    if _is_stale_packet(path, payload):
        lines.extend([
            "- packet_status: stale",
            "- fallback_to: checkpoint/summary/task-local refs",
        ])
        return lines + [""]
    for key in ("ready_to_continue", "continue_mode", "next_action"):
        if key in payload:
            lines.append(f"- {key}: {_short(payload[key], max_chars)}")
    lines.extend(_packet_progress_lines(payload, max_chars))
    paths = payload.get("recommended_read_paths")
    if isinstance(paths, list) and paths:
        lines.append("- recommended_read_paths:")
        lines.extend(f"  - {_short(item, max_chars)}" for item in paths[:5])
    lines.extend(_packet_read_policy_lines(payload))
    return lines + [""]


# LLM: _packet_progress_lines gives runners the useful part of packet state without reading the full JSON.
# 函数用途: 展示 work_progress 的摘要、最近写入路径和标题；没有进度时明确告诉 runner 可以直接开始任务。
def _packet_progress_lines(payload: dict[str, Any], max_chars: int) -> list[str]:
    progress = payload.get("work_progress")
    if not isinstance(progress, dict) or not progress:
        return ["- work_progress: none"]
    lines = ["- work_progress:"]
    for key in ("summary", "latest_written_path", "next_action", "latest_tool_progress_ref"):
        value = progress.get(key)
        if str(value or "").strip():
            lines.append(f"  - {key}: {_short(value, max_chars)}")
    headings = progress.get("headings")
    if isinstance(headings, list) and headings:
        lines.append("  - headings:")
        lines.extend(f"    - {_short(item, max_chars)}" for item in headings[:8])
    return lines


# LLM: _packet_read_policy_lines prevents empty packets from wasting model turns.
# 函数用途: 如果 packet 已经被 prompt 摘要过且没有实际进度，提示 runner 不要反复读完整 JSON。
def _packet_read_policy_lines(payload: dict[str, Any]) -> list[str]:
    progress = payload.get("work_progress")
    session = payload.get("session_compact")
    has_progress = isinstance(progress, dict) and bool(progress)
    has_session = isinstance(session, dict) and bool(session)
    if has_progress or has_session:
        return ["- packet_read_policy: read specific refs only when the summary is insufficient."]
    return [
        "- packet_read_policy: packet is already summarized here; "
        "because work_progress/session_compact are empty, start from the task goal instead of reading the packet body.",
    ]


# LLM: _session_compact_lines shows subagent-local compact metadata without reading main memory.
# 函数用途: 从 compactions/latest_metadata.json 和 latest_summary.md 渲染本地 compact 包摘要。
def _session_compact_lines(refs: dict[str, str], max_chars: int) -> list[str]:
    metadata_ref = refs.get("agent_run_latest_session_compaction_metadata", "")
    summary_ref = refs.get("agent_run_latest_session_compaction_summary", "")
    if not metadata_ref and not summary_ref:
        metadata_ref = _legacy_session_metadata_ref(refs)
        summary_ref = _legacy_session_summary_ref(refs) if metadata_ref else ""
    if not metadata_ref and not summary_ref:
        return []
    payload, status = _read_json_with_status(Path(metadata_ref)) if metadata_ref else ({}, "missing")
    lines = ["### Session Compact Package", ""]
    lines.extend(_compact_ref_lines(metadata_ref, summary_ref))
    lines.extend(_compact_metadata_bullets(payload, status, max_chars))
    summary = _read_text(Path(summary_ref), max_chars) if summary_ref else ""
    if summary:
        lines.extend(["", summary])
    return lines + [""]


# LLM: _legacy_session_metadata_ref keeps old workspaces readable only when their latest metadata is a session package.
# 函数用途: 兼容迁移前把 session compact 写到 compactions/latest_metadata.json 的旧任务，避免误把 checkpoint 当 session。
def _legacy_session_metadata_ref(refs: dict[str, str]) -> str:
    metadata_ref = refs.get("agent_run_latest_compaction_metadata", "")
    if not metadata_ref:
        return ""
    payload, status = _read_json_with_status(Path(metadata_ref))
    if status == "ok" and payload.get("schema_version") == "subagent_session_compact.v1":
        return metadata_ref
    return ""


# LLM: _legacy_session_summary_ref pairs with legacy metadata only after the metadata schema check passes.
# 函数用途: 返回旧 session summary 路径；新任务应使用 agent_run_latest_session_compaction_summary。
def _legacy_session_summary_ref(refs: dict[str, str]) -> str:
    return refs.get("agent_run_latest_compaction_summary", "")


# LLM: _compact_ref_lines keeps session compact refs rendering flat and reusable.
# 函数用途: 渲染 latest metadata/summary 路径行，减少主 prompt 拼装函数嵌套。
def _compact_ref_lines(metadata_ref: str, summary_ref: str) -> list[str]:
    lines: list[str] = []
    if metadata_ref:
        lines.append(f"- latest_metadata: {metadata_ref}")
    if summary_ref:
        lines.append(f"- latest_summary: {summary_ref}")
    return lines


# LLM: _compact_metadata_bullets extracts small scalar metadata hints from the local compact package.
# 函数用途: 渲染 compact metadata 的关键字段；坏 metadata 只展示状态，不抛错。
def _compact_metadata_bullets(payload: dict[str, Any], status: str, max_chars: int) -> list[str]:
    if status != "ok":
        return [f"- metadata_status: {status}"]
    keys = ("schema_version", "memory_scope", "writes_main_memory", "current_step", "next_action")
    return [f"- {key}: {_short(payload[key], max_chars)}" for key in keys if key in payload]


# LLM: _preflight_lines keeps packet self-healing auditable after prepare_runner_attempt regenerates files.
# 函数用途: 展示 runner 启动前看到的 corrupt/missing/stale packet 状态，并明确降级到 checkpoint/summary。
def _preflight_lines(context_bundle: dict[str, object], max_chars: int) -> list[str]:
    if not isinstance(context_bundle, dict):
        return []
    reserved = context_bundle.get("reserved") if isinstance(context_bundle.get("reserved"), dict) else {}
    preflight = reserved.get("runner_recovery_preflight") if isinstance(reserved, dict) else None
    if not isinstance(preflight, dict):
        return []
    status = str(preflight.get("packet_status") or "unknown")
    lines = [
        "### Recovery Preflight",
        "",
        f"- packet_status_before_prepare: {_short(status, max_chars)}",
        f"- packet_ref_before_prepare: {_short(preflight.get('packet_ref', ''), max_chars)}",
        "- fallback_to: checkpoint/summary/task-local refs",
    ]
    instruction = str(preflight.get("runner_instruction") or "").strip()
    if instruction:
        lines.append(f"- runner_instruction: {_short(instruction, max_chars)}")
    refs = preflight.get("fallback_refs")
    if isinstance(refs, list) and refs:
        lines.append("- fallback_refs:")
        lines.extend(f"  - {_short(item, max_chars)}" for item in refs[:5])
    if preflight.get("save_may_regenerate_continue_packet") is True:
        lines.append("- prepare_note: runner prepare may regenerate latest_continue_packet after this preflight.")
    return lines + [""]


# LLM: _ref_lines lists durable task-local files before snippets so readers can inspect originals.
# 函数用途: 给 runner 和父级状态一个稳定路径清单，正文很长时仍可按路径追溯。
def _ref_lines(refs: dict[str, str]) -> list[str]:
    if not refs:
        return []
    lines = ["### Recovery Refs", ""]
    lines.extend(f"- {key}: {value}" for key, value in refs.items())
    return lines + [""]


# LLM: _snippet_lines gives tiny human-readable state hints while keeping artifact bodies external.
# 函数用途: 读取 checkpoint/summary/task 等小片段，帮助压缩后的子代理知道从哪继续。
def _snippet_lines(refs: dict[str, str], max_chars: int) -> list[str]:
    lines: list[str] = []
    for key in ("agent_run_checkpoint", "agent_run_summary", "agent_run_task", "agent_run_findings"):
        text = _read_text(Path(refs[key]), max_chars) if key in refs else ""
        if text:
            lines.extend([f"### {key}", "", text, ""])
    return lines


# LLM: _read_json is intentionally forgiving because corrupted packets should not crash prompt rendering.
# 函数用途: 容错读取 JSON 对象，失败时返回空对象并让其它 refs 继续可用。
def _read_json(path: Path) -> dict[str, Any]:
    payload, _status = _read_json_with_status(path)
    return payload


# LLM: _read_json_with_status lets prompt builders explain corrupted packet fallback without throwing.
# 函数用途: 容错读取 JSON，并返回 ok/unreadable_json 状态供恢复提示明确降级原因。
def _read_json_with_status(path: Path) -> tuple[dict[str, Any], str]:
    try:
        raw = path.read_text(encoding="utf-8")
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return {}, "unreadable_json"
    if len(raw) > _MAX_PACKET_JSON_CHARS:
        return {}, "unreadable_json"
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return {}, "unreadable_json"
    return (payload, "ok") if isinstance(payload, dict) else ({}, "unreadable_json")


# LLM: _is_stale_packet uses packet created_at when present, otherwise filesystem mtime as a conservative fallback.
# 函数用途: 判断 continue packet 是否过期；过期时不信任 next_action，只把 checkpoint/summary 作为恢复事实源。
def _is_stale_packet(path: Path, payload: dict[str, Any]) -> bool:
    timestamp = _packet_timestamp(path, payload)
    return timestamp > 0 and time.time() - timestamp > _STALE_PACKET_SECONDS


# LLM: _packet_timestamp tolerates old packets without created_at by using mtime.
# 函数用途: 从 packet 字段或文件 mtime 得到可比较时间戳，失败时返回 0。
def _packet_timestamp(path: Path, payload: dict[str, Any]) -> float:
    raw = payload.get("created_at")
    try:
        value = float(raw)
    except (TypeError, ValueError):
        try:
            value = path.stat().st_mtime
        except OSError:
            return 0.0
    return max(0.0, value)


# LLM: _read_text bounds every file snippet so a huge log cannot flood the runner prompt.
# 函数用途: 安全读取短文本片段；超长内容追加截断提示，原文仍通过 refs 可追溯。
def _read_text(path: Path, max_chars: int) -> str:
    try:
        raw = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return ""
    text = _pretty_json_text(raw) if path.suffix == ".json" else raw
    limit = max(200, int(max_chars or _DEFAULT_SNIPPET_CHARS))
    if len(text) <= limit:
        return text.strip()
    return f"{text[:limit].rstrip()}\n... [truncated; read ref for full content]"


# LLM: _pretty_json_text keeps escaped unicode readable to the runner and reviewers.
# 函数用途: 对 JSON 恢复片段做可读化渲染；解析失败时保留原文，避免损坏文件导致 prompt 失败。
def _pretty_json_text(raw: str) -> str:
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return raw
    return json.dumps(payload, ensure_ascii=False, indent=2)


# LLM: _short keeps scalar packet fields readable inside one prompt bullet.
# 函数用途: 把 continue packet 的值压成单行摘要，避免列表或对象撑大 prompt。
def _short(value: Any, max_chars: int) -> str:
    text = json.dumps(value, ensure_ascii=False) if isinstance(value, (dict, list)) else str(value)
    limit = max(80, min(int(max_chars or _DEFAULT_SNIPPET_CHARS), 300))
    return text if len(text) <= limit else f"{text[:limit].rstrip()}..."


__all__ = ["SubagentCompactContinuationRequest", "build_subagent_compact_continuation_section"]
