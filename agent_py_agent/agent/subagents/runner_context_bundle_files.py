
from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path

from .context_bundle import (
    ContextBundleV1,
    ContextGateReport,
    build_context_bundle,
    render_context_bundle_markdown,
    validate_context_bundle,
)
from .models import SubAgentExecutionContext, SubAgentTask


def execution_context_bundle(task: SubAgentTask) -> dict[str, object]:
    bundle = build_context_bundle(task)
    gate = validate_context_bundle(bundle)
    payload = asdict(bundle)
    payload["gate"] = asdict(gate)
    payload["context_bundle_json"] = str(Path(task.task_dir) / "context_bundle.json")
    payload["context_bundle_file"] = str(Path(task.task_dir) / "CONTEXT_BUNDLE.md")
    return payload


def write_context_bundle_files(context: SubAgentExecutionContext) -> None:
    payload = dict(context.context_bundle or {})
    bundle_payload = {
        key: value
        for key, value in payload.items()
        if key not in {"gate", "context_bundle_json", "context_bundle_file"}
    }
    gate_payload = payload.get("gate") or {}
    bundle_json = Path(context.context_bundle_json)
    bundle_json.parent.mkdir(parents=True, exist_ok=True)
    bundle_json.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    bundle = build_context_bundle_from_payload(bundle_payload)
    gate = context_gate_report_from_payload(gate_payload)
    bundle_file = Path(context.context_bundle_file)
    bundle_file.parent.mkdir(parents=True, exist_ok=True)
    bundle_file.write_text(render_context_bundle_markdown(bundle, gate), encoding="utf-8")
    _mirror_context_bundle_to_run_workspace(context)


def _mirror_context_bundle_to_run_workspace(context: SubAgentExecutionContext) -> None:
    refs = context.context_bundle.get("workspace_refs") if isinstance(context.context_bundle, dict) else {}
    if not isinstance(refs, dict):
        return
    run_workspace = str(refs.get("agent_work_dir") or refs.get("agent_run_workspace") or "").strip()
    if not run_workspace:
        return
    target_dir = Path(run_workspace)
    target_dir.mkdir(parents=True, exist_ok=True)
    json_target = target_dir / "context_bundle.json"
    md_target = target_dir / "CONTEXT_BUNDLE.md"
    json_target.write_text(Path(context.context_bundle_json).read_text(encoding="utf-8"), encoding="utf-8")
    md_target.write_text(Path(context.context_bundle_file).read_text(encoding="utf-8"), encoding="utf-8")


def build_context_bundle_from_payload(payload: object):
    if not isinstance(payload, dict):
        payload = {}
    return ContextBundleV1(**payload)


def context_gate_report_from_payload(payload: object):
    if not isinstance(payload, dict):
        payload = {}
    return ContextGateReport(**payload)
