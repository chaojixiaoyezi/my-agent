# LLM: Read-only agent tree status keeps inspection separate from dispatch/progress mutation.
# 模块用途: 把主代理、子代理、孙代理状态整理成 refs-first 树形快照；不触发调度、恢复或验收。

from __future__ import annotations

from ..subagents.kernel import SubagentKernelQuery
from .agent_tree_node_rendering import node_from_kernel_run
from .agent_tree_progress import attach_task_progress
from .agent_tree_scope_filter import (
    coordination_advice,
    scope_main_fallback_snapshot,
    status_buckets,
    visible_nodes,
)
from .orchestration_child_result_index import child_result_index_from_nodes
from .orchestration_run_scope import remembered_orchestration_run_ids
from .orchestration_scope_resolution import scope_resolution_payload, tree_scope_resolution
from .runner_context import current_subagent_run_id

_SCHEMA_VERSION = "agent_tree_status.v1"


# LLM: agent_tree_status_payload is the single read model behind inspect_agent_tree.
# 函数用途: 返回整棵代理树状态；只读读取 manager.kernel_snapshot，不修改 pending_work 或任务文件。
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
    advice = coordination_advice(nodes)
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
        "source_refs": dict(snapshot.source_refs),
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

# LLM: _kernel_snapshot keeps query derivation structural and free of natural-language parsing.
# 函数用途: 从显式 run/root 参数或当前轮已知 run_ids 选择状态树；缺省返回 manager 可见 run。
def _kernel_snapshot(agent: object, query: SubagentKernelQuery):
    manager = getattr(agent, "subagents", None)
    if manager is None or not callable(getattr(type(manager), "kernel_snapshot", None)):
        return _empty_snapshot()
    return manager.kernel_snapshot(query)


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
    progress_layer = {
        "progress": 0.0,
        "current_step": "",
        "current_tool": current_tool,
        "last_progress_at": last_progress_at,
        "last_progress_summary": last_progress_summary,
        "latest_summary": "",
    }
    attach_task_progress(agent, run_id, progress_layer)
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

# LLM: _empty_snapshot returns a safe read-only kernel response when no manager exists.
# 函数用途: 生成空任务树快照，避免状态查看因为缺 subagent manager 崩溃。
def _empty_snapshot():
    from ..subagents.kernel import SubagentKernelSnapshot

    return SubagentKernelSnapshot(
        schema_version="subagent_kernel_snapshot.v1",
        scope="root_tree",
        warnings=["subagent_manager_unavailable"],
    )


__all__ = ["agent_tree_status_payload"]
