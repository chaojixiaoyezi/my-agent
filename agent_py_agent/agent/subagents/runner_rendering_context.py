# LLM: Render context-bundle refs for execution-context markdown without growing runner_rendering.py.
# 模块用途: 把子代理实时工单包的文件引用和 gate 状态渲染成 Markdown 小节。

from __future__ import annotations

from .context_bundle import context_gate_prompt_lines
from .models import SubAgentExecutionContext


# LLM: render_context_bundle_section exposes bundle refs without embedding oversized task history.
# 函数用途: 在 EXECUTION_CONTEXT.md 展示 context bundle 文件位置和 gate 状态，方便 runner/接管代理定位实时工单包。
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
        f"- agent_run_workspace: {refs.get('agent_run_workspace') or 'none'}",
        f"- agent_run_context_bundle_json: {_run_workspace_bundle_ref(refs, 'context_bundle.json')}",
        f"- agent_run_context_bundle_file: {_run_workspace_bundle_ref(refs, 'CONTEXT_BUNDLE.md')}",
    ]
    lines.extend(context_gate_prompt_lines(context.context_bundle))
    return lines


# LLM: _run_workspace_bundle_ref derives the mirrored bundle path from the workspace ref.
# 函数用途: 给 Markdown 展示 agent run workspace 内的 context bundle 镜像路径，不读取文件。
def _run_workspace_bundle_ref(refs: dict[str, object], name: str) -> str:
    workspace = str(refs.get("agent_run_workspace") or "").strip()
    if not workspace:
        return "none"
    return f"{workspace.rstrip('/')}/{name}"
