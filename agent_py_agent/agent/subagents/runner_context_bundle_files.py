# LLM: Runner context bundle file persistence is separate from execution-context assembly.
# 模块用途: 生成并写出 context_bundle.json / CONTEXT_BUNDLE.md，同时镜像到 agent run workspace。

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


# LLM: execution_context_bundle embeds the gate report beside handoff facts for runner self-checks.
# 函数用途: 生成执行上下文内的 context_bundle 字典，包含 bundle 正文和 gate 结果。
def execution_context_bundle(task: SubAgentTask) -> dict[str, object]:
    bundle = build_context_bundle(task)
    gate = validate_context_bundle(bundle)
    payload = asdict(bundle)
    payload["gate"] = asdict(gate)
    payload["context_bundle_json"] = str(Path(task.task_dir) / "context_bundle.json")
    payload["context_bundle_file"] = str(Path(task.task_dir) / "CONTEXT_BUNDLE.md")
    return payload


# LLM: write_context_bundle_files persists the handoff bundle next to execution context files.
# 函数用途: 写出 context_bundle.json 和 CONTEXT_BUNDLE.md；不改变任务状态，只补充可读交接物。
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


# LLM: _mirror_context_bundle_to_run_workspace writes the bundle to the canonical agent_work_dir.
# 函数用途: 把 context bundle 同步到当前 agent 工作目录，供接管和 compact 读取。
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


# LLM: build_context_bundle_from_payload keeps Markdown rendering decoupled from dataclass serialization.
# 函数用途: 从已序列化字典恢复 ContextBundleV1；只用于同进程落盘渲染。
def build_context_bundle_from_payload(payload: object):
    if not isinstance(payload, dict):
        payload = {}
    return ContextBundleV1(**payload)


# LLM: context_gate_report_from_payload keeps gate Markdown rendering tolerant of missing future fields.
# 函数用途: 从 gate 字典恢复 ContextGateReport；只用于 context bundle 文件渲染。
def context_gate_report_from_payload(payload: object):
    if not isinstance(payload, dict):
        payload = {}
    return ContextGateReport(**payload)
