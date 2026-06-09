
from __future__ import annotations

"""Takeover readiness packet builders for failed or blocked subagent runs."""

import json
from pathlib import Path
from typing import Any

from ....common.value_parsing import sequence_strings
from ...models import SubAgentTask

_BOUNDARY_NOTES = [
    "summary_is_not_verified_fact",
    "finding_requires_evidence_packet_or_acceptance",
    "artifact_ref_is_pointer_not_prompt_content",
    "blackboard_is_not_source_of_truth",
]


def build_takeover_readiness_packet(task: SubAgentTask) -> dict[str, object]:
    manifest_records = _read_manifest_records(task.agent_run_artifact_manifest_jsonl)
    read_order = _recommended_read_order(task)
    return {
        "schema_name": "subagent_takeover_readiness_packet",
        "schema_version": 1,
        "run": _run_identity(task),
        "status": task.status,
        "current_step": task.current_step or task.status,
        "latest_summary": task.latest_summary,
        "blockers": _unique_strings(task.blockers),
        "failure_handoff_ref": task.failure_handoff_json if task.failure_handoff.run_id else "",
        "context_bundle_refs": _context_bundle_refs(task),
        "checkpoint_refs": _checkpoint_refs(task),
        "artifact_refs": _unique_strings(task.artifact_refs),
        "artifact_manifest_ref": task.agent_run_artifact_manifest_jsonl or task.task_artifact_manifest_jsonl,
        "artifact_manifest_records": manifest_records,
        "evidence_refs": _unique_strings(task.evidence_refs),
        "status_report_ref": task.status_report_json,
        "recommended_next_action": _recommended_next_action(task),
        "recommended_read_order": read_order,
        "boundary_notes": list(_BOUNDARY_NOTES),
        "auto_takeover": False,
        "reads_artifact_bodies": False,
        "packet_is_recovery_index": True,
    }


def render_takeover_readiness_markdown(packet: dict[str, object]) -> str:
    run = _dict_value(packet.get("run"))
    checkpoint_refs = _dict_value(packet.get("checkpoint_refs"))
    context_bundle_refs = _dict_value(packet.get("context_bundle_refs"))
    return "\n".join(
        [
            "# TAKEOVER_READINESS",
            "",
            f"- run_id: {run.get('run_id', '')}",
            f"- parent_id: {run.get('parent_id', '') or 'none'}",
            f"- root_id: {run.get('root_id', '') or 'none'}",
            f"- status: {packet.get('status', '')}",
            f"- current_step: {packet.get('current_step', '')}",
            f"- failure_handoff_ref: {packet.get('failure_handoff_ref', '') or 'none'}",
            f"- context_bundle: {context_bundle_refs.get('agent_run_context_bundle', '') or 'none'}",
            f"- task_checkpoint: {checkpoint_refs.get('task_checkpoint', '') or 'none'}",
            f"- agent_run_checkpoint: {checkpoint_refs.get('agent_run_checkpoint', '') or 'none'}",
            "",
            "## 建议读取顺序",
            _render_list(sequence_strings(packet.get("recommended_read_order"))),
            "",
            "## Context Bundle Refs",
            _render_key_values(context_bundle_refs),
            "",
            "## 阻塞项",
            _render_list(sequence_strings(packet.get("blockers"))),
            "",
            "## Artifact Refs",
            _render_list(sequence_strings(packet.get("artifact_refs"))),
            "",
            "## Artifact Manifest",
            f"- manifest: {packet.get('artifact_manifest_ref', '') or 'none'}",
            _render_manifest_rows(_list_dicts(packet.get("artifact_manifest_records"))),
            "",
            "## 边界提示",
            _render_list(sequence_strings(packet.get("boundary_notes"))),
            "",
        ]
    )


def write_takeover_readiness_files(task: SubAgentTask) -> dict[str, object]:
    packet = build_takeover_readiness_packet(task)
    if task.takeover_readiness_json:
        Path(task.takeover_readiness_json).write_text(
            json.dumps(packet, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    if task.takeover_readiness_md:
        Path(task.takeover_readiness_md).write_text(render_takeover_readiness_markdown(packet), encoding="utf-8")
    return packet


def takeover_readiness_ref_order(path_text: str) -> list[str]:
    if not path_text:
        return []
    path = Path(path_text)
    refs = [str(path)]
    try:
        packet = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return refs
    if isinstance(packet, dict):
        refs.extend(sequence_strings(packet.get("recommended_read_order")))
    return _unique_strings(refs)


def _run_identity(task: SubAgentTask) -> dict[str, object]:
    return {
        "run_id": task.id,
        "parent_id": task.parent_id,
        "root_id": task.root_id or task.id,
        "task_dir": task.task_dir,
        "agent_run_workspace_dir": task.agent_run_workspace_dir,
    }


def _checkpoint_refs(task: SubAgentTask) -> dict[str, str]:
    return {
        "task_checkpoint": task.checkpoint_json or task.checkpoint_ref,
        "agent_run_checkpoint": task.agent_run_checkpoint_json,
        "compact_metadata": task.agent_run_latest_compaction_metadata_json,
        "compact_summary": task.agent_run_latest_compaction_summary_md,
    }


def _context_bundle_refs(task: SubAgentTask) -> dict[str, str]:
    agent_run_root = Path(task.agent_run_workspace_dir) if task.agent_run_workspace_dir else None
    return {
        "task_context_bundle": str(agent_run_root / "context_bundle.json") if agent_run_root else "",
        "task_context_bundle_md": str(agent_run_root / "CONTEXT_BUNDLE.md") if agent_run_root else "",
        "agent_run_context_bundle": str(agent_run_root / "context_bundle.json") if agent_run_root else "",
        "agent_run_context_bundle_md": str(agent_run_root / "CONTEXT_BUNDLE.md") if agent_run_root else "",
    }


def _recommended_read_order(task: SubAgentTask) -> list[str]:
    context_refs = _context_bundle_refs(task)
    refs = [
        task.failure_handoff_json if task.failure_handoff.run_id else "",
        task.takeover_readiness_json,
        context_refs.get("agent_run_context_bundle", ""),
        context_refs.get("task_context_bundle", ""),
        task.agent_run_checkpoint_json,
        task.checkpoint_json or task.checkpoint_ref,
        task.status_report_json,
        task.agent_run_artifact_manifest_jsonl or task.task_artifact_manifest_jsonl,
        *task.evidence_refs,
        *task.artifact_refs,
    ]
    return _unique_strings(refs)


def _recommended_next_action(task: SubAgentTask) -> str:
    if task.failure_handoff.recommended_next_action:
        return task.failure_handoff.recommended_next_action
    if task.latest_status_report.next_recommended_action:
        return task.latest_status_report.next_recommended_action
    if task.blockers:
        return f"先处理阻塞项：{task.blockers[0]}"
    return "按 recommended_read_order 读取 refs 后再决定是否接管。"


def _read_manifest_records(path_text: str) -> list[dict[str, object]]:
    if not path_text:
        return []
    path = Path(path_text)
    if not path.exists():
        return []
    records: list[dict[str, object]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(item, dict):
            records.append(_safe_manifest_record(item))
    return records


def _safe_manifest_record(item: dict[str, object]) -> dict[str, object]:
    keys = [
        "ref",
        "path",
        "exists",
        "size_bytes",
        "sha256",
        "content_externalized",
        "resolution_status",
    ]
    return {
        key: item.get(key, "" if key not in {"exists", "content_externalized", "size_bytes"} else False)
        for key in keys
    }


def _unique_strings(values: list[str]) -> list[str]:
    return list(dict.fromkeys(item for item in values if item))


def _dict_value(value: object) -> dict[str, object]:
    return value if isinstance(value, dict) else {}


def _list_dicts(value: object) -> list[dict[str, object]]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


def _render_list(items: list[str]) -> str:
    return "\n".join(f"- {item}" for item in items) if items else "- 暂无"


def _render_key_values(items: dict[str, object]) -> str:
    if not items:
        return "- 暂无"
    return "\n".join(f"- {key}: {value or 'none'}" for key, value in items.items())


def _render_manifest_rows(records: list[dict[str, Any]]) -> str:
    if not records:
        return "- records: 暂无"
    rows = []
    for item in records:
        rows.append(
            "- "
            f"ref={item.get('ref', '')}; "
            f"exists={item.get('exists', False)}; "
            f"size={item.get('size_bytes', 0)}; "
            f"sha256={item.get('sha256', '')}"
        )
    return "\n".join(rows)
