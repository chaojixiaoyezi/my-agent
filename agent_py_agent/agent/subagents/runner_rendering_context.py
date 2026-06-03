
from __future__ import annotations

from .context_bundle import context_gate_prompt_lines
from .models import SubAgentExecutionContext


def render_context_bundle_section(context: SubAgentExecutionContext) -> list[str]:
    refs = context.context_bundle.get("workspace_refs") if isinstance(context.context_bundle, dict) else {}
    if not isinstance(refs, dict):
        refs = {}
    lines = [
        "",
        "## Context Bundle",
        "",
        f"- context_bundle_json: {context.context_bundle_json or 'none'}",
        f"- context_bundle_file: {context.context_bundle_file or 'none'}",
        f"- agent_work_dir: {refs.get('agent_work_dir') or 'none'}",
        f"- agent_run_context_bundle_json: {_run_workspace_bundle_ref(refs, 'context_bundle.json')}",
        f"- agent_run_context_bundle_file: {_run_workspace_bundle_ref(refs, 'CONTEXT_BUNDLE.md')}",
    ]
    lines.extend(context_gate_prompt_lines(context.context_bundle))
    return lines


def _run_workspace_bundle_ref(refs: dict[str, object], name: str) -> str:
    workspace = str(refs.get("agent_work_dir") or refs.get("agent_run_workspace") or "").strip()
    if not workspace:
        return "none"
    return f"{workspace.rstrip('/')}/{name}"
