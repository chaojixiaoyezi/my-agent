# LLM: Read-only agent tree status keeps inspection separate from dispatch/progress mutation.
# 模块用途: 把主代理、子代理、孙代理状态整理成 refs-first 树形快照；不触发调度、恢复或验收。

from __future__ import annotations

from dataclasses import asdict
from typing import Any

from ..subagents.kernel import SubagentKernelQuery
from .orchestration_run_scope import remembered_orchestration_run_ids

_SCHEMA_VERSION = "agent_tree_status.v1"


# LLM: agent_tree_status_payload is the single read model behind inspect_agent_tree.
# 函数用途: 返回整棵代理树状态；只读读取 manager.kernel_snapshot，不修改 pending_work 或任务文件。
def agent_tree_status_payload(agent: object, params: dict[str, object] | None = None) -> dict[str, object]:
    params = params or {}
    snapshot = _kernel_snapshot(agent, params)
    nodes = [_node_from_kernel_run(row) for row in snapshot.runs]
    main = _main_agent_node(agent, nodes)
    return {
        "schema_version": _SCHEMA_VERSION,
        "effect": "read_only",
        "scope": snapshot.scope,
        "root_id": snapshot.root_id,
        "main": main,
        "nodes": nodes,
        "edges": _tree_edges(nodes),
        "status_buckets": {
            "running": list(snapshot.running_run_ids),
            "blocked": list(snapshot.blocked_run_ids),
            "completed": list(snapshot.completed_run_ids),
            "failed": list(snapshot.failed_run_ids),
            "takeover_candidates": list(snapshot.takeover_candidate_run_ids),
        },
        "source_refs": dict(snapshot.source_refs),
        "warnings": list(snapshot.warnings),
        "policy": {
            "read_only": True,
            "does_not_dispatch": True,
            "does_not_clear_pending_work": True,
            "next_step": "如果只是查看状态，直接向用户汇报；只有用户要推进或恢复时才调用 dispatch_subagents。",
        },
    }


# LLM: _kernel_snapshot keeps query derivation structural and free of natural-language parsing.
# 函数用途: 从显式 run/root 参数或当前轮已知 run_ids 选择状态树；缺省返回 manager 可见 run。
def _kernel_snapshot(agent: object, params: dict[str, object]):
    manager = getattr(agent, "subagents", None)
    if manager is None or not callable(getattr(type(manager), "kernel_snapshot", None)):
        return _empty_snapshot()
    root_id = str(params.get("root_id") or "").strip()
    run_id = str(params.get("run_id") or "").strip()
    scope = str(params.get("scope") or "").strip() or ("own_subtree" if run_id else "root_tree")
    if not root_id and not run_id:
        run_id = _first_remembered_run_id(agent)
    return manager.kernel_snapshot(SubagentKernelQuery(root_id=root_id, run_id=run_id, scope=scope))


# LLM: _main_agent_node synthesizes the foreground agent row without pretending it is a subagent task.
# 函数用途: 给整棵树补一个主代理节点；状态来自运行时标记，子节点来自没有 parent 的可见 run。
def _main_agent_node(agent: object, nodes: list[dict[str, object]]) -> dict[str, object]:
    run_id = str(getattr(agent, "_main_agent_run_id", "") or "main")
    status = "PENDING_WORK" if bool(getattr(agent, "has_pending_work", False)) else "IDLE"
    current_tool = str(getattr(agent, "_current_tool", "") or "")
    heartbeat_at = float(getattr(agent, "_heartbeat_at", 0.0) or 0.0)
    last_progress_at = float(getattr(agent, "_last_progress_at", 0.0) or 0.0)
    last_progress_summary = str(getattr(agent, "_last_progress_summary", "") or "")
    child_run_ids = [
        str(item.get("run_id") or "")
        for item in nodes
        if not str(item.get("parent_run_id") or "")
    ]
    return {
        "task_id": run_id,
        "run_id": run_id,
        "parent_id": "",
        "parent_run_id": "",
        "parent_task_id": "",
        "root_run_id": run_id,
        "depth": 0,
        "agent_kind": "main_agent",
        "status": status,
        "current_tool": current_tool,
        "heartbeat_at": heartbeat_at,
        "last_progress_at": last_progress_at,
        "last_progress_summary": last_progress_summary,
        "artifact_refs": [],
        "blockers": [],
        "child_run_ids": [item for item in child_run_ids if item],
        "liveness": {"status": status, "heartbeat_at": heartbeat_at, "updated_at": heartbeat_at, "has_heartbeat": bool(heartbeat_at)},
        "progress_layer": {
            "progress": 0.0,
            "current_step": "",
            "current_tool": current_tool,
            "last_progress_at": last_progress_at,
            "last_progress_summary": last_progress_summary,
            "latest_summary": "",
        },
        "evidence_layer": {
            "artifact_refs": [],
            "evidence_refs": [],
            "blockers": [],
            "needs_capability": [],
            "recent_tool_trace": [],
        },
    }


# LLM: _node_from_kernel_run exposes small status facts and refs only.
# 函数用途: 将 kernel run 行转成模型可读状态节点，不读取产物正文。
def _node_from_kernel_run(row: object) -> dict[str, object]:
    payload = asdict(row)
    tool_contract = _dict(payload.get("tool_contract"))
    reserved = _dict(payload.get("reserved"))
    artifact_refs = _list(payload.get("artifact_refs"))
    evidence_refs = _list(payload.get("evidence_refs"))
    blockers = _list(payload.get("blockers"))
    needs_capability = _needs_capability(tool_contract, reserved)
    recent_tool_trace = _recent_tool_trace(reserved)
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
        "artifact_refs": artifact_refs,
        "evidence_refs": evidence_refs,
        "blockers": blockers,
        "workspace_refs": payload.get("workspace_refs", {}),
        "recovery_refs": payload.get("recovery_refs", {}),
        "tool_contract": tool_contract,
        "needs_capability": needs_capability,
        "recent_tool_trace": recent_tool_trace,
        "liveness": {
            "status": payload.get("status", ""),
            "heartbeat_at": payload.get("heartbeat_at", 0.0),
            "updated_at": payload.get("updated_at", 0.0),
            "has_heartbeat": bool(payload.get("heartbeat_at", 0.0)),
        },
        "progress_layer": {
            "progress": payload.get("progress", 0.0),
            "current_step": payload.get("current_step", ""),
            "current_tool": payload.get("current_tool", ""),
            "last_progress_at": payload.get("last_progress_at", 0.0),
            "last_progress_summary": payload.get("last_progress_summary", ""),
            "latest_summary": payload.get("latest_summary", ""),
        },
        "evidence_layer": {
            "artifact_refs": artifact_refs,
            "evidence_refs": evidence_refs,
            "blockers": blockers,
            "needs_capability": needs_capability,
            "recent_tool_trace": recent_tool_trace,
        },
    }


# LLM: _tree_edges renders parent-child links without requiring callers to infer them from row order.
# 函数用途: 为状态树返回显式边，主代理到 root run 的边也在这里补齐。
def _tree_edges(nodes: list[dict[str, object]]) -> list[dict[str, str]]:
    edges: list[dict[str, str]] = []
    for node in nodes:
        run_id = str(node.get("run_id") or "")
        parent = str(node.get("parent_run_id") or "")
        if not run_id:
            continue
        edges.append({"from": parent or "main", "to": run_id})
    return edges


def _first_remembered_run_id(agent: object) -> str:
    for run_id in remembered_orchestration_run_ids(agent):
        text = str(run_id or "").strip()
        if text:
            return text
    return ""


def _empty_snapshot():
    from ..subagents.kernel import SubagentKernelSnapshot

    return SubagentKernelSnapshot(
        schema_version="subagent_kernel_snapshot.v1",
        scope="root_tree",
        warnings=["subagent_manager_unavailable"],
    )


__all__ = ["agent_tree_status_payload"]


def _dict(value: object) -> dict[str, object]:
    return dict(value) if isinstance(value, dict) else {}


def _list(value: object) -> list:
    return list(value) if isinstance(value, list) else []


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
    result: list[dict[str, object]] = []
    for item in value[-5:]:
        if isinstance(item, dict):
            result.append(dict(item))
    return result


def _safe_int(value: object) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0
