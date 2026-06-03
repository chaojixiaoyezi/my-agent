from __future__ import annotations

"""Shared progress helpers for CLI status surfaces."""

import json
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Any

from ..agent.common.json_io import read_json_object_report
from ..agent.common.value_parsing import string_list
from ..agent.local_storage import AgentRuntimeQueryContext


def shared_progress_for_board(agent: Any, board: Any, *, purpose: str) -> list[dict[str, Any]]:
    existing = getattr(board, "shared_progress", None)
    if isinstance(existing, list):
        return existing
    panels = _query_panels(agent, _root_ids_from_board(board), purpose)
    try:
        board.shared_progress = panels
    except Exception:
        # Cache attachment is best-effort; panels are returned as the source of truth.
        pass
    return panels


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
        if entry.get("takeover_readiness_load_error"):
            error = entry["takeover_readiness_load_error"]
            lines.append(
                f"  - takeover_readiness_load_error={error.get('context')} "
                f"category={error.get('category')}"
            )
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


def _root_ids_from_board(board: Any) -> list[str]:
    roots: list[str] = []
    for item in [*list(getattr(board, "hot_list", []) or []), *list(getattr(board, "recent", []) or [])]:
        root_id = str(getattr(item, "root_id", "") or getattr(item, "id", ""))
        if root_id and root_id not in roots:
            roots.append(root_id)
    return roots


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


def _list_attr(source: Any, name: str) -> list[Any]:
    value = getattr(source, name, [])
    return list(value) if isinstance(value, (list, tuple)) else []


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


def _takeover_entries(runs: list[Any]) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    for run in runs:
        failure_ref = str(run.metadata.get("failure_handoff_ref") or "")
        readiness_ref = str(run.metadata.get("takeover_readiness_ref") or "")
        if not failure_ref and not readiness_ref:
            continue
        read_order, readiness_load_error = _read_takeover_read_order_report(readiness_ref, failure_ref)
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
            "recommended_read_order": read_order,
            "takeover_readiness_load_error": readiness_load_error,
        })
    return entries


def _takeover_entries_from_panels(panels: list[dict[str, Any]]) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    for panel in panels:
        raw_entries = panel.get("takeover_entries", [])
        if isinstance(raw_entries, list):
            entries.extend(item for item in raw_entries if isinstance(item, dict))
    return entries


def _dict_metadata(run: Any, key: str) -> dict[str, Any]:
    value = run.metadata.get(key)
    return dict(value) if isinstance(value, dict) else {}


def _read_takeover_read_order(readiness_ref: str, failure_ref: str) -> list[str]:
    return _read_takeover_read_order_report(readiness_ref, failure_ref)[0]


def _read_takeover_read_order_report(readiness_ref: str, failure_ref: str) -> tuple[list[str], dict[str, object] | None]:
    refs = [readiness_ref, failure_ref]
    if readiness_ref:
        report = read_json_object_report(
            Path(readiness_ref),
            context="cli.shared_progress.takeover_readiness.read",
        )
        if report.payload:
            refs = [readiness_ref, *_string_list(report.payload.get("recommended_read_order")), failure_ref]
        return _unique_strings(refs), report.load_error
    return _unique_strings(refs), None


def _failure_handoff_refs(runs: list[Any]) -> list[str]:
    refs: list[str] = []
    for run in runs:
        ref = str(run.metadata.get("failure_handoff_ref") or "")
        if ref and ref not in refs:
            refs.append(ref)
    return refs


def _takeover_readiness_refs(runs: list[Any]) -> list[str]:
    refs: list[str] = []
    for run in runs:
        ref = str(run.metadata.get("takeover_readiness_ref") or "")
        if ref and ref not in refs:
            refs.append(ref)
    return refs


def _string_list(value: object) -> list[str]:
    return string_list(value)


def _unique_strings(values: list[str]) -> list[str]:
    return list(dict.fromkeys(item for item in values if item))
