
from __future__ import annotations

"""LLM: 这里只渲染 context bundle 的模型视图，持久 JSON 继续保留完整 owner-scoped refs。

模块用途: 把主代理上下文包压成短提示或可读 Markdown；任务晋升前不把 owner 根目录误报成产物 cwd。
"""

import json


# LLM: Pending conversation turns may read owner-scoped refs, but the model-facing summary must
# not advertise owner home as the turn cwd. The canonical bundle on disk remains unchanged.
# 函数用途: 渲染给模型看的短上下文；尚未开始任务时隐藏容易被复制成产物路径的宿主绝对目录。
def render_prompt_section(bundle: dict[str, object], *, json_path: str) -> str:
    workspace = dict(bundle.get("workspace_refs") or {})
    memory = dict(bundle.get("memory_refs") or {})
    recovery = dict(bundle.get("recovery_refs") or {})
    pending_workspace = _pending_conversation_workspace(bundle)
    primary_workspace = (
        "当前 owner 私人空间（只用相对路径读取；首个工作工具会固定任务目录）"
        if pending_workspace
        else workspace.get("primary_workspace_root") or "(unknown)"
    )
    my_agent_home = (
        "(internal)" if pending_workspace else workspace.get("my_agent_home") or "(unavailable)"
    )
    context_bundle_ref = (
        "(internal owner-scoped snapshot)" if pending_workspace else json_path or "(ephemeral)"
    )
    lines = [
        "# Main Agent Context Bundle v1",
        "- 这是主代理本轮运行的结构化上下文（context bundle，给模型看的任务交接包）。",
        "- 大文件、工具输出和历史正文只通过路径引用；需要正文时再显式读取。",
        "- 本轮 request/run/task 等运行标识只供 runtime 内部关联，完整值保存在 context_bundle_json；"
        "不得把它们当成用户可见的会话任务编号。",
        f"- primary_workspace_root: {primary_workspace}",
        f"- my_agent_home: {my_agent_home}",
        f"- related_memory_count: {memory.get('related_memory_count', 0)}",
        f"- resume_context_injected: {str(recovery.get('resume_context_injected', False)).lower()}",
        f"- context_bundle_json: {context_bundle_ref}",
        f"- self_check_ok: {str(dict(bundle.get('self_check') or {}).get('ok', False)).lower()}",
    ]
    return _fit_prompt_budget("\n".join(lines), bundle)


# LLM: This predicate reads only typed task attributes. It must never inspect user prose to decide
# whether work exists; a conversation thread without a bound run_workspace is structurally pending.
# 函数用途: 判断本轮是否还只是普通会话、尚未由工作工具建立正式任务目录。
def _pending_conversation_workspace(bundle: dict[str, object]) -> bool:
    task = bundle.get("task")
    task = task if isinstance(task, dict) else {}
    attributes = task.get("attributes")
    attributes = attributes if isinstance(attributes, dict) else {}
    return bool(
        str(attributes.get("conversation_thread_id") or "").strip()
        and not isinstance(attributes.get("run_workspace"), dict)
    )


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


def _fit_prompt_budget(text: str, bundle: dict[str, object]) -> str:
    budget = bundle.get("prompt_budget", {}) if isinstance(bundle.get("prompt_budget"), dict) else {}
    maximum = int(budget.get("max_prompt_section_chars", 1600) or 1600)
    if len(text) <= maximum:
        return text
    suffix = "\n- prompt_section_truncated: true，完整 JSON 请读 context_bundle_json。"
    return text[: max(0, maximum - len(suffix))].rstrip() + suffix


def _json_block(value: object) -> str:
    return "```json\n" + json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n```"


__all__ = ["render_markdown_bundle", "render_prompt_section"]
