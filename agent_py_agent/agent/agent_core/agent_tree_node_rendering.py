# LLM: Agent tree node rendering keeps inspect_agent_tree thin and read-only.
# 模块用途: 将 kernel run 行投影成模型可读节点；只返回 refs 和状态摘要，不读取产物正文。

from __future__ import annotations

from dataclasses import asdict
from typing import Any

from .agent_tree_progress import attach_task_progress


# LLM: node_from_kernel_run exposes small status facts and refs only.
# 函数用途: 将 kernel run 行转成模型可读状态节点，不读取产物正文。
def node_from_kernel_run(agent: object, row: object) -> dict[str, object]:
    payload = asdict(row)
    refs = _node_ref_values(payload)
    node = _node_identity(payload)
    node.update(_node_status(payload, refs))
    node["liveness"] = _liveness_layer(payload)
    node["progress_layer"] = _progress_layer(agent, payload)
    node["evidence_layer"] = _evidence_layer(refs)
    node["guidance_layer"] = _guidance_layer(agent, str(node.get("run_id") or ""))
    return node


# LLM: _node_ref_values groups refs and capability signals before node rendering.
# 函数用途: 从 kernel payload 提取 artifact/evidence/blocker 和最近工具轨迹。
def _node_ref_values(payload: dict[str, object]) -> dict[str, list[object]]:
    tool_contract = _dict(payload.get("tool_contract"))
    reserved = _dict(payload.get("reserved"))
    return {
        "artifact_refs": _list(payload.get("artifact_refs")),
        "artifact_registry_refs": _dict_list(payload.get("artifact_registry_refs")),
        "evidence_refs": _list(payload.get("evidence_refs")),
        "blockers": _list(payload.get("blockers")),
        "needs_capability": _needs_capability(tool_contract, reserved),
        "recent_tool_trace": _recent_tool_trace(reserved),
    }


# LLM: _node_identity keeps identity and lifecycle fields together.
# 函数用途: 生成任务树节点的身份、状态、进度和层级基础字段。
def _node_identity(payload: dict[str, object]) -> dict[str, object]:
    return {
        "task_id": payload.get("task_id") or payload.get("run_id", ""),
        "run_id": payload.get("run_id", ""),
        "parent_id": payload.get("parent_id", ""),
        "parent_task_id": payload.get("parent_task_id", ""),
        "parent_run_id": payload.get("parent_run_id", ""),
        "root_id": payload.get("root_id", ""),
        "root_run_id": payload.get("root_run_id", ""),
        "depth": payload.get("depth", 0),
        "agent_kind": payload.get("agent_kind", ""),
        "role": payload.get("role", ""),
        "agent_name": payload.get("agent_name", ""),
        "status": payload.get("status", ""),
        "verification_status": payload.get("verification_status", ""),
        "failure_type": payload.get("failure_type", ""),
        "progress": payload.get("progress", 0.0),
        "current_step": payload.get("current_step", ""),
        "current_tool": payload.get("current_tool", ""),
        "heartbeat_at": payload.get("heartbeat_at", 0.0),
        "updated_at": payload.get("updated_at", 0.0),
        "last_progress_at": payload.get("last_progress_at", 0.0),
        "last_progress_summary": payload.get("last_progress_summary", ""),
        "latest_summary": payload.get("latest_summary", ""),
        "child_ids": payload.get("child_ids", []),
    }


# LLM: _node_status attaches refs and tool contract facts without reading artifact bodies.
# 函数用途: 给任务树节点补充 workspace、recovery、capability 和最近工具状态。
def _node_status(payload: dict[str, object], refs: dict[str, list[object]]) -> dict[str, object]:
    return {
        "artifact_refs": refs["artifact_refs"],
        "artifact_registry_refs": refs["artifact_registry_refs"],
        "evidence_refs": refs["evidence_refs"],
        "blockers": refs["blockers"],
        "workspace_refs": _workspace_refs(payload.get("workspace_refs")),
        "recovery_refs": payload.get("recovery_refs", {}),
        "tool_contract": _dict(payload.get("tool_contract")),
        "needs_capability": refs["needs_capability"],
        "recent_tool_trace": refs["recent_tool_trace"],
    }


# LLM: _liveness_layer keeps heartbeat facts separate from progress and evidence.
# 函数用途: 渲染节点存活状态，供父代理判断运行中、停滞或缺心跳。
def _liveness_layer(payload: dict[str, object]) -> dict[str, object]:
    heartbeat_at = payload.get("heartbeat_at", 0.0)
    return {
        "status": payload.get("status", ""),
        "heartbeat_at": heartbeat_at,
        "updated_at": payload.get("updated_at", 0.0),
        "has_heartbeat": bool(heartbeat_at),
    }


# LLM: _progress_layer attaches persisted task_progress without expanding artifacts.
# 函数用途: 给 tree 节点补充进度摘要、当前工具和最近进展。
def _progress_layer(agent: object, payload: dict[str, object]) -> dict[str, object]:
    layer = {
        "progress": payload.get("progress", 0.0),
        "current_step": payload.get("current_step", ""),
        "current_tool": payload.get("current_tool", ""),
        "last_progress_at": payload.get("last_progress_at", 0.0),
        "last_progress_summary": payload.get("last_progress_summary", ""),
        "latest_summary": payload.get("latest_summary", ""),
    }
    attach_task_progress(agent, str(payload.get("run_id") or ""), layer)
    return layer


# LLM: _workspace_refs hides legacy paths from model-visible tree payloads.
# 函数用途: 过滤旧 subagent 路径，只保留当前任务工作区和 run 工作区引用。
def _workspace_refs(value: object) -> dict[str, object]:
    refs = _dict(value)
    return {key: item for key, item in refs.items() if key not in {"legacy_task_dir", "legacy_output_json"}}


# LLM: _evidence_layer groups refs and blockers without reading their bodies.
# 函数用途: 渲染产物、证据、能力缺口和最近工具轨迹层。
def _evidence_layer(values: dict[str, list[object]]) -> dict[str, object]:
    return {
        "artifact_refs": values["artifact_refs"],
        "artifact_registry_refs": values["artifact_registry_refs"],
        "evidence_refs": values["evidence_refs"],
        "blockers": values["blockers"],
        "needs_capability": values["needs_capability"],
        "recent_tool_trace": values["recent_tool_trace"],
    }


# LLM: _guidance_layer makes pending soft steering visible in inspect_agent_tree.
# 函数用途: 读取某个 run 的未投递 guidance 摘要；只读展示，不标记已读。
def _guidance_layer(agent: object, run_id: str) -> dict[str, object]:
    store = getattr(agent, "conversation_store", None)
    if store is None or not run_id:
        return {"pending_count": 0, "recent_pending": []}
    try:
        pending = list(store.pending_guidance("agent_run", run_id, limit=5))
    except Exception:
        return {"pending_count": 0, "recent_pending": [], "warnings": ["guidance_unavailable"]}
    return {
        "pending_count": len(pending),
        "recent_pending": [_guidance_item(item) for item in pending],
    }


# LLM: _guidance_item renders a bounded guidance row for parent visibility.
# 函数用途: 将 guidance 账本行裁剪成 id、消息、优先级和发送者。
def _guidance_item(item: object) -> dict[str, object]:
    return {
        "guidance_id": str(getattr(item, "guidance_id", "") or ""),
        "message": str(getattr(item, "message", "") or ""),
        "priority": str(getattr(item, "priority", "") or "normal"),
        "sender": str(getattr(item, "sender", "") or ""),
    }


# LLM: _dict is a defensive projection helper for loose kernel payload fields.
# 函数用途: 非 dict 值按空字典处理，避免 tree 渲染中断。
def _dict(value: object) -> dict[str, object]:
    return dict(value) if isinstance(value, dict) else {}


# LLM: _list keeps list-like payload fields bounded to actual lists.
# 函数用途: 非 list 值按空列表处理，避免字符串被拆成字符。
def _list(value: object) -> list:
    return list(value) if isinstance(value, list) else []


# LLM: _dict_list filters registry rows to JSON object entries only.
# 函数用途: 只保留 dict 形式的 artifact registry 引用。
def _dict_list(value: object) -> list[dict[str, object]]:
    if not isinstance(value, list):
        return []
    return [dict(item) for item in value if isinstance(item, dict)]


# LLM: _needs_capability derives visible capability gaps from structured tool facts.
# 函数用途: 把能力申请、授权缺口和显式 needs_capability 汇总给父代理。
def _needs_capability(tool_contract: dict[str, object], reserved: dict[str, object]) -> list[str]:
    explicit = reserved.get("needs_capability")
    if isinstance(explicit, list):
        return [str(item) for item in explicit if str(item or "").strip()]
    needs: list[str] = []
    if _safe_int(tool_contract.get("open_request_count")) > 0:
        needs.append("capability_request")
    if _safe_int(tool_contract.get("gap_count")) > 0:
        needs.append("capability_gap")
    return needs


# LLM: _recent_tool_trace exposes only the latest small tool trace rows.
# 函数用途: 从 reserved 中裁剪最近工具轨迹，避免 tree 输出膨胀。
def _recent_tool_trace(reserved: dict[str, object]) -> list[dict[str, object]]:
    value = reserved.get("recent_tool_trace")
    if not isinstance(value, list):
        return []
    return [dict(item) for item in value[-5:] if isinstance(item, dict)]


# LLM: _safe_int keeps malformed counters from breaking status rendering.
# 函数用途: 将计数字段安全转成 int，坏值按 0 处理。
def _safe_int(value: object) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


__all__ = ["node_from_kernel_run"]
