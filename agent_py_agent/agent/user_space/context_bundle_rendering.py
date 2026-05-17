# LLM: Context bundle rendering keeps prompt/Markdown formatting out of bundle assembly.
# 模块用途: 渲染主代理 context bundle 的 prompt 摘要和 Markdown 镜像，避免核心生成文件继续膨胀。

from __future__ import annotations

import json


# LLM: render_prompt_section is intentionally short because detailed bodies stay behind refs.
# 函数用途: 渲染给模型看的简短 context bundle 摘要，保证 prompt 里有稳定机器字段。
def render_prompt_section(bundle: dict[str, object], *, json_path: str) -> str:
    scope = dict(bundle.get("scope") or {})
    workspace = dict(bundle.get("workspace_refs") or {})
    memory = dict(bundle.get("memory_refs") or {})
    recovery = dict(bundle.get("recovery_refs") or {})
    lines = [
        "# Main Agent Context Bundle v1",
        "- 这是主代理本轮运行的结构化上下文（context bundle，给模型看的任务交接包）。",
        "- 大文件、工具输出和历史正文只通过路径引用；需要正文时再显式读取。",
        f"- request_id: {scope.get('request_id') or '(empty)'}",
        f"- run_id: {scope.get('run_id') or '(empty)'}",
        f"- task_id: {scope.get('task_id') or '(empty)'}",
        f"- primary_workspace_root: {workspace.get('primary_workspace_root') or '(unknown)'}",
        f"- my_agent_home: {workspace.get('my_agent_home') or '(unavailable)'}",
        f"- related_memory_count: {memory.get('related_memory_count', 0)}",
        f"- resume_context_injected: {str(recovery.get('resume_context_injected', False)).lower()}",
        f"- context_bundle_json: {json_path or '(ephemeral)'}",
        f"- self_check_ok: {str(dict(bundle.get('self_check') or {}).get('ok', False)).lower()}",
    ]
    return _fit_prompt_budget("\n".join(lines), bundle)


# LLM: render_markdown_bundle renders a human-readable mirror of the same machine payload.
# 函数用途: 写给人看的 context bundle 说明，方便调试而不需要打开 JSON。
def render_markdown_bundle(bundle: dict[str, object], *, json_path: str) -> str:
    return "\n".join(
        [
            "# Main Agent Context Bundle v1",
            "",
            f"- JSON: {json_path}",
            f"- schema: {bundle.get('schema')}",
            f"- created_at: {bundle.get('created_at')}",
            "",
            "## Scope",
            _json_block(bundle.get("scope") or {}),
            "",
            "## Workspace Refs",
            _json_block(bundle.get("workspace_refs") or {}),
            "",
            "## Memory Refs",
            _json_block(bundle.get("memory_refs") or {}),
            "",
            "## Recovery Refs",
            _json_block(bundle.get("recovery_refs") or {}),
            "",
            "## Run Scope",
            _json_block(bundle.get("run_scope") or {}),
            "",
            "## Tool Manifest",
            _json_block(bundle.get("tool_manifest") or {}),
            "",
            "## Acceptance Contract",
            _json_block(bundle.get("acceptance_contract") or {}),
            "",
            "## Self Check",
            _json_block(bundle.get("self_check") or {}),
            "",
        ]
    )


# LLM: _fit_prompt_budget keeps the injected section bounded while full JSON stays on disk.
# 函数用途: 如果摘要超过预算，保留前段核心字段并追加截断说明。
def _fit_prompt_budget(text: str, bundle: dict[str, object]) -> str:
    budget = bundle.get("prompt_budget", {}) if isinstance(bundle.get("prompt_budget"), dict) else {}
    maximum = int(budget.get("max_prompt_section_chars", 1600) or 1600)
    if len(text) <= maximum:
        return text
    suffix = "\n- prompt_section_truncated: true，完整 JSON 请读 context_bundle_json。"
    return text[: max(0, maximum - len(suffix))].rstrip() + suffix


# LLM: _json_block centralizes deterministic Markdown JSON rendering.
# 函数用途: 在 Markdown 镜像里输出排序后的 JSON 片段，方便人和 LLM 对照。
def _json_block(value: object) -> str:
    return "```json\n" + json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n```"


__all__ = ["render_markdown_bundle", "render_prompt_section"]
