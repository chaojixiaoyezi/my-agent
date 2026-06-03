
from __future__ import annotations

from ...model_visible_refs import current_model_ref
from ...runtime_errors import runtime_error_report
from ...subagents.kernel import SubagentKernelQuery
from ..orchestration.child_result_index import child_result_index_from_nodes
from ..orchestration.run_scope import remembered_orchestration_run_ids
from ..orchestration.scope_resolution import scope_resolution_payload, tree_scope_resolution
from ..runner.context import current_subagent_run_id
from .node_rendering import node_from_kernel_run
from .progress import attach_task_progress
from .scope_filter import (
    coordination_advice,
    scope_main_fallback_snapshot,
    status_buckets,
    visible_nodes,
)

_SCHEMA_VERSION = "agent_tree_status.v1"


def agent_tree_status_payload(agent: object, params: dict[str, object] | None = None) -> dict[str, object]:
    params = params or {}
    query = _kernel_query(agent, params)
    snapshot = _kernel_snapshot(agent, query)
    warnings = list(snapshot.warnings)
    if not snapshot.runs and _is_main_run_query(agent, query):
        snapshot = _kernel_snapshot(agent, SubagentKernelQuery(scope="root_tree"))
        snapshot = scope_main_fallback_snapshot(agent, snapshot, remembered_orchestration_run_ids(agent))
        warnings.extend(["main_run_scope_had_no_subagent_rows_returned_visible_tree", *list(snapshot.warnings)])
    resolution = tree_scope_resolution(
        agent,
        params,
        effective_run_id=query.run_id,
        effective_root_id=query.root_id or snapshot.root_id,
        effective_scope=query.scope,
    )
    nodes = [node_from_kernel_run(agent, row) for row in snapshot.runs]
    nodes = visible_nodes(nodes, params.get("visible_run_ids"))
    main = _main_agent_node(agent, nodes)
    advice = coordination_advice(nodes, params.get("allowed_tools"))
    payload = {
        "schema_version": _SCHEMA_VERSION,
        "effect": "read_only",
        "scope": snapshot.scope,
        "root_id": snapshot.root_id,
        "main": main,
        "nodes": nodes,
        "edges": _tree_edges(nodes),
        "child_result_index": child_result_index_from_nodes(nodes),
        "status_buckets": status_buckets(nodes),
        "coordination_advice": advice,
        "source_refs": _current_source_refs(snapshot.source_refs),
        "warnings": [*warnings, *resolution.warnings],
        "policy": {
            "read_only": True,
            "does_not_dispatch": True,
            "does_not_clear_pending_work": True,
            "next_step": advice["next_step_zh"],
            "scope_resolution": resolution.to_dict(),
        },
    }
    payload.update(scope_resolution_payload(resolution))
    return payload


def _current_source_refs(source_refs: object) -> dict[str, object]:
    if not isinstance(source_refs, dict):
        return {}
    return {
        str(key): projected
        for key, value in source_refs.items()
        if (projected := _current_source_ref_value(value))
    }


def _current_source_ref_value(value: object) -> object:
    if isinstance(value, list | tuple | set):
        return [ref for item in value if (ref := current_model_ref(item))]
    return current_model_ref(value)


def _kernel_snapshot(agent: object, query: SubagentKernelQuery):
    manager = getattr(agent, "subagents", None)
    if manager is None or not callable(getattr(type(manager), "kernel_snapshot", None)):
        return _empty_snapshot()
    try:
        return manager.kernel_snapshot(query)
    except Exception as exc:
        return _empty_snapshot(runtime_error_report(exc, context="subagent_kernel.snapshot"))


def _kernel_query(agent: object, params: dict[str, object]) -> SubagentKernelQuery:
    current_run_id = current_subagent_run_id(agent)
    if current_run_id:
        return SubagentKernelQuery(run_id=current_run_id, scope="own_subtree")
    root_id = str(params.get("root_id") or "").strip()
    run_id = str(params.get("run_id") or "").strip()
    scope = str(params.get("scope") or "").strip() or ("own_subtree" if run_id else "root_tree")
    return SubagentKernelQuery(root_id=root_id, run_id=run_id, scope=scope)


def _is_main_run_query(agent: object, query: SubagentKernelQuery) -> bool:
    main_run_id = str(getattr(agent, "_main_agent_run_id", "") or "").strip()
    query_id = query.run_id or query.root_id
    if main_run_id and query_id == main_run_id:
        return True
    return query_id.startswith("run-")


def _main_agent_node(agent: object, nodes: list[dict[str, object]]) -> dict[str, object]:
    run_id = str(getattr(agent, "_main_agent_run_id", "") or "main")
    status = "PENDING_WORK" if bool(getattr(agent, "has_pending_work", False)) else "IDLE"
    current_tool = str(getattr(agent, "_current_tool", "") or "")
    heartbeat_at = float(getattr(agent, "_heartbeat_at", 0.0) or 0.0)
    last_progress_at = float(getattr(agent, "_last_progress_at", 0.0) or 0.0)
    last_progress_summary = str(getattr(agent, "_last_progress_summary", "") or "")
    child_run_ids = _main_child_run_ids(nodes)
    progress_facts = {
        "current_tool": current_tool,
        "last_progress_at": last_progress_at,
        "last_progress_summary": last_progress_summary,
    }
    progress_layer = _main_progress_layer(agent, run_id, progress_facts)
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
        "artifact_registry_refs": [],
        "blockers": [],
        "child_run_ids": [item for item in child_run_ids if item],
        "liveness": {"status": status, "heartbeat_at": heartbeat_at, "updated_at": heartbeat_at, "has_heartbeat": bool(heartbeat_at)},
        "progress_layer": progress_layer,
        "evidence_layer": {
            "artifact_refs": [],
            "artifact_registry_refs": [],
            "evidence_refs": [],
            "blockers": [],
            "needs_capability": [],
            "recent_tool_trace": [],
        },
    }


def _main_child_run_ids(nodes: list[dict[str, object]]) -> list[str]:
    return [
        run_id
        for item in nodes
        if not str(item.get("parent_run_id") or "")
        if (run_id := str(item.get("run_id") or ""))
    ]


def _main_progress_layer(agent: object, run_id: str, progress_facts: dict[str, object]) -> dict[str, object]:
    progress_layer: dict[str, object] = {
        "progress": 0.0,
        "current_step": "",
        "current_tool": progress_facts["current_tool"],
        "last_progress_at": progress_facts["last_progress_at"],
        "last_progress_summary": progress_facts["last_progress_summary"],
        "latest_summary": "",
    }
    attach_task_progress(agent, run_id, progress_layer)
    return progress_layer


def _tree_edges(nodes: list[dict[str, object]]) -> list[dict[str, str]]:
    edges: list[dict[str, str]] = []
    for node in nodes:
        run_id = str(node.get("run_id") or "")
        parent = str(node.get("parent_run_id") or "")
        if not run_id:
            continue
        edges.append({"from": parent or "main", "to": run_id})
    return edges


def _empty_snapshot(error_report: dict[str, object] | None = None):
    from ...subagents.kernel import SubagentKernelSnapshot

    warnings = ["subagent_manager_unavailable"]
    reserved: dict[str, object] = {}
    if error_report:
        warnings = ["subagent_kernel_error"]
        reserved["load_errors"] = [error_report]
    return SubagentKernelSnapshot(
        schema_version="subagent_kernel_snapshot.v1",
        scope="root_tree",
        warnings=warnings,
        reserved=reserved,
    )


__all__ = ["agent_tree_status_payload"]
