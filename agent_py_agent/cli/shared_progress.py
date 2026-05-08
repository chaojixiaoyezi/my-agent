# LLM: CLI shared-progress helpers render LocalStore control-plane projections without loading artifact bodies.
# 模块用途: 给 status/subagents CLI 生成共享进度面板摘要，失败交接只展示引用。

from __future__ import annotations

"""Shared progress helpers for CLI status surfaces."""

from dataclasses import asdict, is_dataclass
from typing import Any

from ..agent.local_storage import AgentRuntimeQueryContext


# LLM: shared_progress_for_board collects root task panels for visible board items only.
# 函数用途: 从看板可见项查询共享进度面板，并返回短 JSON 摘要。
def shared_progress_for_board(agent: Any, board: Any, *, purpose: str) -> list[dict[str, Any]]:
    existing = getattr(board, "shared_progress", None)
    if isinstance(existing, list):
        return existing
    panels = _query_panels(agent, _root_ids_from_board(board), purpose)
    try:
        board.shared_progress = panels
    except Exception:
        pass
    return panels


# LLM: format_shared_progress_lines keeps human CLI output compact and refs-only.
# 函数用途: 把共享进度面板摘要渲染成人类可扫读的短行。
def format_shared_progress_lines(panels: list[dict[str, Any]]) -> list[str]:
    if not panels:
        return ["- 暂无"]
    lines: list[str] = []
    for panel in panels:
        lines.append(
            f"- {panel['root_task_id']} runs={panel['run_count']} "
            f"blocked={panel['blocked_count']} failure_handoffs={len(panel['failure_handoff_refs'])} "
            f"takeover_packets={len(panel.get('takeover_readiness_refs', []))}"
        )
    return lines


# LLM: _query_panels keeps CLI status as a read-only projection over LocalStore panels.
# 函数用途: 按 root task 查询共享进度面板并转成 CLI 可序列化摘要。
def _query_panels(agent: Any, root_ids: list[str], purpose: str) -> list[dict[str, Any]]:
    local_store = getattr(agent, "local_store", None)
    if not local_store or not hasattr(local_store, "query_shared_progress_panel"):
        return []
    panels: list[dict[str, Any]] = []
    for root_id in root_ids:
        panel = local_store.query_shared_progress_panel(
            AgentRuntimeQueryContext(root_task_id=root_id, scope="root_tree", purpose=purpose, requester_role="cli")
        )
        panels.append(_panel_payload(panel))
    return panels


# LLM: _root_ids_from_board deduplicates visible board roots before querying the control plane.
# 函数用途: 从 hot/recent 看板项里提取 root_id，避免重复查询同一棵任务树。
def _root_ids_from_board(board: Any) -> list[str]:
    roots: list[str] = []
    for item in [*list(getattr(board, "hot_list", []) or []), *list(getattr(board, "recent", []) or [])]:
        root_id = str(getattr(item, "root_id", "") or getattr(item, "id", ""))
        if root_id and root_id not in roots:
            roots.append(root_id)
    return roots


# LLM: _panel_payload serializes a SharedProgressPanel without reading linked files.
# 函数用途: 把共享进度面板转成 status/subagents 输出需要的短 JSON。
def _panel_payload(panel: Any) -> dict[str, Any]:
    runs = _list_attr(panel, "runs")
    blocked_runs = _list_attr(panel, "blocked_runs")
    rollup = getattr(panel, "rollup", None)
    failure_refs = list(getattr(panel, "failure_handoff_refs", []) or _failure_handoff_refs(runs))
    takeover_refs = list(getattr(panel, "takeover_readiness_refs", []) or _takeover_readiness_refs(runs))
    return {
        "root_task_id": str(getattr(getattr(panel, "context", None), "root_task_id", "") or ""),
        "rollup": asdict(rollup) if is_dataclass(rollup) else None,
        "run_count": len(runs),
        "blocked_count": len(blocked_runs),
        "blocked_runs": [_run_payload(item) for item in blocked_runs],
        "inheritance_manifest_refs": list(getattr(panel, "inheritance_manifest_refs", []) or []),
        "failure_handoff_refs": failure_refs,
        "takeover_readiness_refs": takeover_refs,
        "warnings": list(getattr(panel, "warnings", []) or []),
    }


# LLM: _list_attr normalizes optional list-like attributes from loose test/runtime objects.
# 函数用途: 安全读取对象上的列表字段，缺失或非列表时返回空列表。
def _list_attr(source: Any, name: str) -> list[Any]:
    value = getattr(source, name, [])
    return list(value) if isinstance(value, (list, tuple)) else []


# LLM: _run_payload exposes one blocked run summary while keeping metadata refs only.
# 函数用途: 渲染单个 run 的 CLI 摘要，不展开 failure/takeover 文件正文。
def _run_payload(run: Any) -> dict[str, Any]:
    return {
        "run_id": run.run_id,
        "status": run.status,
        "current_step": run.current_step,
        "latest_summary": run.latest_summary,
        "failure_handoff_ref": str(run.metadata.get("failure_handoff_ref") or ""),
        "takeover_readiness_ref": str(run.metadata.get("takeover_readiness_ref") or ""),
    }


# LLM: _failure_handoff_refs collects metadata refs from visible runs without opening files.
# 函数用途: 收集 failure handoff 引用并去重，供共享进度摘要计数。
def _failure_handoff_refs(runs: list[Any]) -> list[str]:
    refs: list[str] = []
    for run in runs:
        ref = str(run.metadata.get("failure_handoff_ref") or "")
        if ref and ref not in refs:
            refs.append(ref)
    return refs


# LLM: _takeover_readiness_refs collects recovery packet refs from visible runs only.
# 函数用途: 收集 takeover readiness 引用并去重，供共享进度摘要计数。
def _takeover_readiness_refs(runs: list[Any]) -> list[str]:
    refs: list[str] = []
    for run in runs:
        ref = str(run.metadata.get("takeover_readiness_ref") or "")
        if ref and ref not in refs:
            refs.append(ref)
    return refs
