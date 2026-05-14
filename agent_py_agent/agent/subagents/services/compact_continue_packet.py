# LLM: Subagent task-local continue packets let parent dispatch resume child runners by refs.
# 模块用途: 生成子代理自己的 latest_continue_packet.json 和 session compact ledger，不写主代理 memory。

from __future__ import annotations

"""Task-local subagent continue packet writer."""

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..models import SubAgentTask

_SCHEMA_VERSION = "subagent_continue_packet.v1"
_CLOSED_STATUSES = {"ABANDONED", "COMPLETED", "TAKEN_OVER"}


# LLM: SubagentContinuePacketRequest keeps packet generation explicit and future-extensible.
# 类用途: 汇总生成子代理继续包所需的任务状态和 runner 输出摘要，避免保存流程继续增加散参数。
@dataclass(frozen=True)
class SubagentContinuePacketRequest:
    task: SubAgentTask
    output_payload: dict[str, object]


# LLM: write_subagent_continue_packet writes only task-local refs and compact ledger rows.
# 函数用途: 在 agent run workspace 的 compactions 目录写 latest_continue_packet.json 和 session_compact_ledger.jsonl。
def write_subagent_continue_packet(request: SubagentContinuePacketRequest) -> str:
    compactions = _compactions_dir(request.task)
    if not compactions:
        return ""
    compactions.mkdir(parents=True, exist_ok=True)
    packet_ref = compactions / "latest_continue_packet.json"
    packet = build_subagent_continue_packet(request, packet_ref)
    packet_ref.write_text(json.dumps(packet, ensure_ascii=False, indent=2), encoding="utf-8")
    _append_packet_ledger(compactions / "session_compact_ledger.jsonl", packet, packet_ref)
    return str(packet_ref)


# LLM: build_subagent_continue_packet returns a refs-first packet safe for parent resume prompts.
# 函数用途: 组装子代理 task-local 恢复包；只包含状态、下一步和路径引用，不展开 artifact 正文。
def build_subagent_continue_packet(request: SubagentContinuePacketRequest, packet_ref: Path) -> dict[str, Any]:
    task = request.task
    restore_refs = _restore_refs(task)
    session_compact = _session_compact_refs(task)
    return {
        "schema_version": _SCHEMA_VERSION,
        "kind": "subagent_task_local_continue_packet",
        "owner": {"owner_type": "subagent_run", "owner_id": task.id},
        "memory_scope": "task_local",
        "writes_main_memory": False,
        "automatic_tool_execution": "none",
        "ready_to_continue": _ready_to_continue(task),
        "continue_mode": "subagent_task_local",
        "run_id": task.id,
        "root_id": task.root_id or task.id,
        "parent_id": task.parent_id,
        "role": task.role,
        "agent_name": task.agent_name,
        "status": task.status,
        "verification_status": task.verification_status,
        "current_step": task.current_step or task.status,
        "latest_summary": task.latest_summary,
        "next_action": _next_action(task, request.output_payload),
        "blockers": _unique_strings([*task.blockers, *_strings(request.output_payload.get("blockers"))]),
        "restore_refs": restore_refs,
        "session_compact": session_compact,
        "recommended_read_paths": _recommended_read_paths(task, packet_ref, restore_refs),
        "guard": _guard_payload(task),
        "reserved": {},
    }


# LLM: _compactions_dir returns the task-local run compactions directory only when wired.
# 函数用途: 查找 agent run workspace 的 compactions 目录；缺少 workspace 时安全跳过写包。
def _compactions_dir(task: SubAgentTask) -> Path | None:
    value = str(getattr(task, "agent_run_compactions_dir", "") or "").strip()
    return Path(value) if value else None


# LLM: _restore_refs maps every stable resume file without checking artifact bodies.
# 函数用途: 输出父级/接管代理可读取的 run workspace、旧工单和共享工作区路径引用。
def _restore_refs(task: SubAgentTask) -> dict[str, str]:
    pairs = {
        "agent_run_workspace": task.agent_run_workspace_dir,
        "agent_run_task": task.agent_run_task_md,
        "agent_run_checkpoint": task.agent_run_checkpoint_json,
        "agent_run_summary": task.agent_run_summary_md,
        "agent_run_final_report": task.agent_run_final_report_md,
        "agent_run_findings": task.agent_run_findings_jsonl,
        "agent_run_timeline": task.agent_run_timeline_jsonl,
        "agent_run_compactions": task.agent_run_compactions_dir,
        "agent_run_latest_compaction_summary": task.agent_run_latest_compaction_summary_md,
        "agent_run_latest_compaction_metadata": task.agent_run_latest_compaction_metadata_json,
        "legacy_task_dir": task.task_dir,
        "legacy_checkpoint": task.checkpoint_json or task.checkpoint_ref,
        "runner_result": task.runner_result_json,
        "output_json": task.output_json,
        "takeover_readiness": task.takeover_readiness_json,
        "shared_blackboard": task.task_workspace_shared_blackboard,
        "shared_messages": task.task_workspace_shared_messages_jsonl,
        "shared_findings": task.task_workspace_shared_findings_jsonl,
    }
    return {key: str(value) for key, value in pairs.items() if str(value or "").strip()}


# LLM: subagent_restore_refs exposes the same refs to package writers without duplicating path rules.
# 函数用途: 给子代理 session compact 写入器复用恢复路径映射，保证 continue packet 和 compact metadata 一致。
def subagent_restore_refs(task: SubAgentTask) -> dict[str, str]:
    return _restore_refs(task)


# LLM: _session_compact_refs summarizes the latest task-local compact package if one exists.
# 函数用途: 把 latest_metadata/latest_summary 挂进 continue packet，父级恢复时能优先按 refs 接续。
def _session_compact_refs(task: SubAgentTask) -> dict[str, str]:
    metadata_ref = str(getattr(task, "agent_run_latest_compaction_metadata_json", "") or "").strip()
    summary_ref = str(getattr(task, "agent_run_latest_compaction_summary_md", "") or "").strip()
    if not metadata_ref or not Path(metadata_ref).exists():
        return {}
    payload = _read_json_object(Path(metadata_ref))
    return {
        "schema_version": str(payload.get("schema_version") or "subagent_session_compact.v1"),
        "package_id": str(payload.get("package_id") or ""),
        "metadata_ref": metadata_ref,
        "summary_ref": summary_ref if summary_ref and Path(summary_ref).exists() else "",
        "next_action": str(payload.get("next_action") or ""),
    }


# LLM: _recommended_read_paths orders the smallest recovery facts before heavy reports.
# 函数用途: 给父级重新 dispatch 或接管时的读取顺序，优先 checkpoint、summary、task 和 output refs。
def _recommended_read_paths(task: SubAgentTask, packet_ref: Path, restore_refs: dict[str, str]) -> list[str]:
    values = [
        str(packet_ref),
        restore_refs.get("agent_run_checkpoint", ""),
        restore_refs.get("agent_run_summary", ""),
        restore_refs.get("agent_run_latest_compaction_metadata", ""),
        restore_refs.get("agent_run_latest_compaction_summary", ""),
        restore_refs.get("agent_run_task", ""),
        restore_refs.get("output_json", ""),
        restore_refs.get("runner_result", ""),
        restore_refs.get("takeover_readiness", ""),
        restore_refs.get("shared_messages", ""),
    ]
    return _unique_strings([item for item in values if _path_exists_or_is_future_ref(item, task)])


# LLM: _read_json_object tolerates absent or corrupt optional compact metadata.
# 函数用途: 读取 latest_metadata.json 时失败返回空对象，让 continue packet 仍能写出。
def _read_json_object(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


# LLM: _path_exists_or_is_future_ref keeps packet refs stable even before optional files appear.
# 函数用途: 过滤明显空路径；保留 latest packet 自身和已存在文件，避免推荐不可用旧路径。
def _path_exists_or_is_future_ref(path_text: str, task: SubAgentTask) -> bool:
    if not path_text:
        return False
    if path_text.endswith("latest_continue_packet.json"):
        return True
    if path_text in {task.output_json, task.runner_result_json}:
        return True
    return Path(path_text).exists()


# LLM: _ready_to_continue is a parent-dispatch signal, not permission to run tools automatically.
# 函数用途: 判断任务是否仍可由父级继续推进；已完成验收或被放弃/接管则不再建议继续。
def _ready_to_continue(task: SubAgentTask) -> bool:
    status = str(task.status or "").upper()
    verification = str(task.verification_status or "").upper()
    if status in _CLOSED_STATUSES:
        return False
    return not (status == "DONE" and verification == "VERIFIED")


# LLM: _next_action selects one concise restart instruction from task and output facts.
# 函数用途: 从 current_step、runner next_actions、status report 和 blocker 中选择父级接续动作。
def _next_action(task: SubAgentTask, output_payload: dict[str, object]) -> str:
    candidates = [
        task.current_step,
        *_strings(output_payload.get("next_actions")),
        output_payload.get("next_action"),
        task.latest_status_report.next_recommended_action if task.latest_status_report else "",
        f"resolve blocker: {task.blockers[0]}" if task.blockers else "",
        "resume runner from task-local checkpoint",
    ]
    return next((str(item).strip() for item in candidates if str(item or "").strip()), "")


# LLM: _guard_payload makes automatic boundaries explicit for future auto-resume code.
# 函数用途: 明确继续包只允许父级调度读取，不允许写主 memory 或直接自动执行工具。
def _guard_payload(task: SubAgentTask) -> dict[str, object]:
    return {
        "allowed_to_continue": _ready_to_continue(task),
        "requires_parent_dispatch": True,
        "automatic_tool_execution": "none",
        "writes_main_memory": False,
    }


# LLM: _append_packet_ledger keeps a small append-only audit row for every generated packet.
# 函数用途: 追加 session compact ledger；行内只保存状态和 packet ref，不复制 packet 正文。
def _append_packet_ledger(path: Path, packet: dict[str, Any], packet_ref: Path) -> None:
    row = {
        "schema_version": "subagent_session_compact_ledger.v1",
        "run_id": packet.get("run_id", ""),
        "root_id": packet.get("root_id", ""),
        "status": packet.get("status", ""),
        "ready_to_continue": packet.get("ready_to_continue", False),
        "packet_ref": str(packet_ref),
        "memory_scope": "task_local",
        "writes_main_memory": False,
    }
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row, ensure_ascii=False) + "\n")


# LLM: _strings normalizes scalar/list values used by output payloads.
# 函数用途: 把 next_actions/blockers 等字段统一成字符串列表，保持顺序和空值过滤。
def _strings(value: object) -> list[str]:
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        return [str(item) for item in value if str(item or "").strip()]
    text = str(value or "").strip()
    return [text] if text else []


# LLM: _unique_strings deduplicates refs and blockers while preserving first-seen order.
# 函数用途: 清理推荐读取路径和 blocker 列表，避免重复路径增加 prompt 噪音。
def _unique_strings(values: list[str]) -> list[str]:
    return list(dict.fromkeys(item for item in values if str(item or "").strip()))


__all__ = [
    "SubagentContinuePacketRequest",
    "build_subagent_continue_packet",
    "subagent_restore_refs",
    "write_subagent_continue_packet",
]
