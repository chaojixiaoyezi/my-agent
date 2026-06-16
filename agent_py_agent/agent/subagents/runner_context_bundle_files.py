
from __future__ import annotations

from dataclasses import asdict
from pathlib import Path

from ..common.json_io import write_json_file_atomic, write_text_file_atomic
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
    # 原子写(temp+replace,同短板4):上下文 bundle"半写即损坏",崩溃中断不能留
    # 半截 JSON/Markdown,否则子代理读到坏 bundle。原子原语自带 parent mkdir。
    bundle_json = Path(context.context_bundle_json)
    write_json_file_atomic(bundle_json, payload, sort_keys=False)
    bundle = build_context_bundle_from_payload(bundle_payload)
    gate = context_gate_report_from_payload(gate_payload)
    bundle_file = Path(context.context_bundle_file)
    write_text_file_atomic(bundle_file, render_context_bundle_markdown(bundle, gate))
    _mirror_context_bundle_to_run_workspace(context)


def _mirror_context_bundle_to_run_workspace(context: SubAgentExecutionContext) -> None:
    refs = context.context_bundle.get("workspace_refs") if isinstance(context.context_bundle, dict) else {}
    if not isinstance(refs, dict):
        return
    run_workspace = str(refs.get("agent_work_dir") or refs.get("agent_run_workspace") or "").strip()
    if not run_workspace:
        return
    target_dir = Path(run_workspace)
    json_target = target_dir / "context_bundle.json"
    md_target = target_dir / "CONTEXT_BUNDLE.md"
    # 镜像到 run workspace 同样原子写(原语自带 parent mkdir),避免半写坏文件。
    write_text_file_atomic(json_target, Path(context.context_bundle_json).read_text(encoding="utf-8"))
    write_text_file_atomic(md_target, Path(context.context_bundle_file).read_text(encoding="utf-8"))


def build_context_bundle_from_payload(payload: object):
    if not isinstance(payload, dict):
        payload = {}
    return ContextBundleV1(**payload)


def context_gate_report_from_payload(payload: object):
    if not isinstance(payload, dict):
        payload = {}
    return ContextGateReport(**payload)
