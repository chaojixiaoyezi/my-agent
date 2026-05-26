# LLM: CLI shared-progress helpers render LocalStore control-plane projections without loading artifact bodies.
# 模块用途:给 status/subagents CLI 生成共享进度面板摘要，失败交接只展示引用。
from __future__ import annotations

"""Shared progress helpers for CLI status surfaces."""

import json
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Any

from ..agent.local_storage import AgentRuntimeQueryContext


# LLM: shared_progress_for_board collects root task panels for visible board items only.
# 函数用途:从看板可见项查询共享进度面板，并返回短 JSON 摘要。
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
# 函数用途:把共享进度面板摘要渲染成人类可扫读的短行。
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


# LLM: format_takeover_view_lines renders concrete recovery entries while keeping artifact bodies out.
# 函数用途:给 status/subagents 输出接管视图，展示 run、handoff、takeover packet 和推荐读序 refs。
def format_takeover_view_lines(panels: list[dict[str, Any]]) -> list[str]:
    entries = _takeover_entries_from_panels(panels)
    if not entries:
        return ["- 暂无"]
    lines: list[str] = []
    for entry in entries:
        lines.append(
            f"- {entry['run_id']} status={entry['status']} "
            f"step={entry['current_step'] or '-'}"
        )
        if entry.get("failure_handoff_ref"):
            lines.append(f"  - failure_handoff={entry['failure_handoff_ref']}")
        if entry.get("takeover_readiness_ref"):
            lines.append(f"  - takeover_readiness={entry['takeover_readiness_ref']}")
        identity = entry.get("runtime_identity") if isinstance(entry.get("runtime_identity"), dict) else {}
        memory = entry.get("memory_scope") if isinstance(entry.get("memory_scope"), dict) else {}
        config = entry.get("config_scope") if isinstance(entry.get("config_scope"), dict) else {}
        if identity or memory or config:
            lines.append(
                f"  - principal={identity.get('effective_principal_id') or '-'} "
                f"conversation={identity.get('conversation_id') or '-'}"
            )
            lines.append(
                f"  - memory={memory.get('namespace') or '-'} "
                f"config={config.get('scope') or '-'} "
                f"global_write={config.get('writes_global_config', False)}"
            )
        for index, ref in enumerate(list(entry.get("recommended_read_order", []) or [])[:5]):
            lines.append(f"  - read_order[{index}]={ref}")
    return lines


# LLM: _query_panels keeps CLI status as a read-only projection over LocalStore panels.
# 函数用途:按 root task 查询共享进度面板并转成 CLI 可序列化摘要。
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
# 函数用途:从 hot/recent 看板项里提取 root_id，避免重复查询同一棵任务树。
def _root_ids_from_board(board: Any) -> list[str]:
    roots: list[str] = []
    for item in [*list(getattr(board, "hot_list", []) or []), *list(getattr(board, "recent", []) or [])]:
        root_id = str(getattr(item, "root_id", "") or getattr(item, "id", ""))
        if root_id and root_id not in roots:
            roots.append(root_id)
    return roots


# LLM: _panel_payload serializes a SharedProgressPanel without reading linked files.
# 函数用途:把共享进度面板转成 status/subagents 输出需要的短 JSON。
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
        "takeover_entries": _takeover_entries(runs),
        "warnings": list(getattr(panel, "warnings", []) or []),
    }


# LLM: _list_attr normalizes optional list-like attributes from loose test/runtime objects.
# 函数用途:安全读取对象上的列表字段，缺失或非列表时返回空列表。
def _list_attr(source: Any, name: str) -> list[Any]:
    value = getattr(source, name, [])
    return list(value) if isinstance(value, (list, tuple)) else []


# LLM: _run_payload exposes one blocked run summary while keeping metadata refs only.
# 函数用途:渲染单个 run 的 CLI 摘要，不展开 failure/takeover 文件正文。
def _run_payload(run: Any) -> dict[str, Any]:
    return {
        "run_id": run.run_id,
        "status": run.status,
        "current_step": run.current_step,
        "latest_summary": run.latest_summary,
        "failure_handoff_ref": str(run.metadata.get("failure_handoff_ref") or ""),
        "takeover_readiness_ref": str(run.metadata.get("takeover_readiness_ref") or ""),
        "runtime_identity": _dict_metadata(run, "runtime_identity"),
        "memory_scope": _dict_metadata(run, "memory_scope"),
        "config_scope": _dict_metadata(run, "config_scope"),
    }


# LLM: _takeover_entries keeps per-run recovery pointers visible without opening large artifact files.
# 函数用途:从 LocalStore run 投影生成接管视图条目，优先读取 takeover packet 的推荐读序。
def _takeover_entries(runs: list[Any]) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    for run in runs:
        failure_ref = str(run.metadata.get("failure_handoff_ref") or "")
        readiness_ref = str(run.metadata.get("takeover_readiness_ref") or "")
        if not failure_ref and not readiness_ref:
            continue
        entries.append({
            "run_id": run.run_id,
            "status": run.status,
            "current_step": run.current_step,
            "latest_summary": run.latest_summary,
            "failure_handoff_ref": failure_ref,
            "takeover_readiness_ref": readiness_ref,
            "runtime_identity": _dict_metadata(run, "runtime_identity"),
            "memory_scope": _dict_metadata(run, "memory_scope"),
            "config_scope": _dict_metadata(run, "config_scope"),
            "recommended_read_order": _read_takeover_read_order(readiness_ref, failure_ref),
        })
    return entries


# LLM: _takeover_entries_from_panels flattens prepared panel payloads for display.
# 函数用途:让 status 和 subagents 共用同一套接管视图渲染输入，避免两个命令各自拼字段。
def _takeover_entries_from_panels(panels: list[dict[str, Any]]) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    for panel in panels:
        raw_entries = panel.get("takeover_entries", [])
        if isinstance(raw_entries, list):
            entries.extend(item for item in raw_entries if isinstance(item, dict))
    return entries


# LLM: _dict_metadata lets status expose scope summaries while rejecting loose non-dict metadata.
# 函数用途:只把控制面里已结构化的身份/记忆/配置 scope 透出到 CLI，不读取外部文件。
def _dict_metadata(run: Any, key: str) -> dict[str, Any]:
    value = run.metadata.get(key)
    return dict(value) if isinstance(value, dict) else {}


# LLM: _read_takeover_read_order reads only the recovery packet JSON index, never artifact body refs.
# 函数用途:从 takeover_readiness.json 取 recommended_read_order，读不到时退回 packet/handoff refs。
def _read_takeover_read_order(readiness_ref: str, failure_ref: str) -> list[str]:
    refs = [readiness_ref, failure_ref]
    if readiness_ref:
        try:
            packet = json.loads(Path(readiness_ref).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            packet = {}
        if isinstance(packet, dict):
            refs = [readiness_ref, *_string_list(packet.get("recommended_read_order")), failure_ref]
    return _unique_strings(refs)


# LLM: _failure_handoff_refs collects metadata refs from visible runs without opening files.
# 函数用途:收集 failure handoff 引用并去重，供共享进度摘要计数。
def _failure_handoff_refs(runs: list[Any]) -> list[str]:
    refs: list[str] = []
    for run in runs:
        ref = str(run.metadata.get("failure_handoff_ref") or "")
        if ref and ref not in refs:
            refs.append(ref)
    return refs


# LLM: _takeover_readiness_refs collects recovery packet refs from visible runs only.
# 函数用途:收集 takeover readiness 引用并去重，供共享进度摘要计数。
def _takeover_readiness_refs(runs: list[Any]) -> list[str]:
    refs: list[str] = []
    for run in runs:
        ref = str(run.metadata.get("takeover_readiness_ref") or "")
        if ref and ref not in refs:
            refs.append(ref)
    return refs


# LLM: _string_list narrows packet fields before status output renders refs.
# 函数用途:只接受 list 里的非空字符串，避免坏 JSON 字段污染接管视图。
def _string_list(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item) for item in value if item not in (None, "")]


# LLM: _unique_strings preserves recovery ref order while removing duplicates.
# 函数用途:让 fallback refs 和 packet recommended_read_order 合并后保持清晰顺序。
def _unique_strings(values: list[str]) -> list[str]:
    return list(dict.fromkeys(item for item in values if item))
