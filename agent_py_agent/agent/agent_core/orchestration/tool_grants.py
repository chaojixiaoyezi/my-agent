from __future__ import annotations

from ...common.value_parsing import TOOL_TEXT_LIST_OPTIONS, string_list
from ...subagents.role_templates import active_model_subagent_tools

CODING_SUBAGENT_TOOLS = [
    "list_files",
    "read_file",
    "search_text",
    "read_artifact",
    "skill_search",
    "web_search",
    "web_fetch",
    "watch_stream",
    "write_file",
    "apply_patch",
    "run_command",
    "create_subagents",
    "send_guidance",
    "cancel_subagents",
    "resolve_capability_requests",
    "capability_request",
]
READ_ONLY_SUBAGENT_TOOLS = [
    "list_files",
    "read_file",
    "search_text",
    "read_artifact",
    "skill_search",
    "web_search",
    "web_fetch",
]
_CODING_TOOL_PRESETS = {"coding"}


# LLM: All ordinary and exact child grants pass the canonical retired-control
# filter; persisted explicit names cannot resurrect deleted model tools.
# 函数用途: 根据显式参数和预设生成 child 工具快照，并剔除旧巡检/推动入口。
def subagent_allowed_tools(params: dict[str, object]) -> list[str] | None:
    allowed_tools = active_model_subagent_tools(
        string_list(params.get("allowed_tools"), TOOL_TEXT_LIST_OPTIONS)
    )
    # LLM: Internal Audit source phases use an exact host-derived grant.  The
    # expected set comes from their typed attributes rather than the caller's
    # list, and is validated again at the final execution seam.
    # 函数用途: 来源子代理从首次绑定前到正式消费都不会被 coding 预设或模型参数重新扩权。
    if params.get("_exact_allowed_tools") is True:
        attrs = params.get("attributes")
        from ...common.audit_activation import (
            audit_worker_tool_scope,
        )

        if exact_scope := audit_worker_tool_scope(attrs):
            return active_model_subagent_tools(exact_scope)
    preset = str(params.get("tool_preset") or "").strip().lower()
    preset_tools = _preset_allowed_tools(preset) if preset else None
    if allowed_tools:
        return _merge_tool_grants(allowed_tools, preset_tools or CODING_SUBAGENT_TOOLS)
    if "tool_preset" not in params:
        return list(CODING_SUBAGENT_TOOLS)
    return _preset_allowed_tools(preset or "read_only") or list(CODING_SUBAGENT_TOOLS)


def _preset_allowed_tools(preset: str) -> list[str] | None:
    if preset in _CODING_TOOL_PRESETS:
        return list(CODING_SUBAGENT_TOOLS)
    if preset == "read_only":
        return list(READ_ONLY_SUBAGENT_TOOLS)
    if preset == "none":
        return None
    return None


# LLM: Merge preserves order but always applies the canonical model-surface filter.
# 函数用途: 合并显式与基础工具，并去重、删除已退休控制工具。
def _merge_tool_grants(explicit: list[str], baseline: list[str] | None) -> list[str]:
    return active_model_subagent_tools([*explicit, *(baseline or [])])
