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


def _liveness_layer(payload: dict[str, object]) -> dict[str, object]:
    heartbeat_at = payload.get("heartbeat_at", 0.0)
    return {
        "status": payload.get("status", ""),
        "heartbeat_at": heartbeat_at,
        "updated_at": payload.get("updated_at", 0.0),
        "has_heartbeat": bool(heartbeat_at),
    }


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


def _workspace_refs(value: object) -> dict[str, object]:
    refs = _dict(value)
    return {key: item for key, item in refs.items() if key not in {"legacy_task_dir", "legacy_output_json"}}


def _evidence_layer(values: dict[str, list[object]]) -> dict[str, object]:
    return {
        "artifact_refs": values["artifact_refs"],
        "artifact_registry_refs": values["artifact_registry_refs"],
        "evidence_refs": values["evidence_refs"],
        "blockers": values["blockers"],
        "needs_capability": values["needs_capability"],
        "recent_tool_trace": values["recent_tool_trace"],
    }


def _dict(value: object) -> dict[str, object]:
    return dict(value) if isinstance(value, dict) else {}


def _list(value: object) -> list:
    return list(value) if isinstance(value, list) else []


def _dict_list(value: object) -> list[dict[str, object]]:
    if not isinstance(value, list):
        return []
    return [dict(item) for item in value if isinstance(item, dict)]


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


def _recent_tool_trace(reserved: dict[str, object]) -> list[dict[str, object]]:
    value = reserved.get("recent_tool_trace")
    if not isinstance(value, list):
        return []
    return [dict(item) for item in value[-5:] if isinstance(item, dict)]


def _safe_int(value: object) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


__all__ = ["node_from_kernel_run"]
