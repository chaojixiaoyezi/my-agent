from __future__ import annotations

from ...common.value_parsing import TOOL_TEXT_LIST_OPTIONS, string_list

CODING_SUBAGENT_TOOLS = [
    "list_files",
    "read_file",
    "search_text",
    "read_artifact",
    "skill_search",
    "web_search",
    "web_fetch",
    "watch_stream",
    "record_finding",
    "write_file",
    "apply_patch",
    "run_command",
    "schedule_child_subagents",
    "dispatch_subagents",
    "inspect_agent_tree",
    "send_guidance",
    "raise_event",
    "raise_collaboration",
    "inspect_collaboration",
    "submit_collaboration_result",
    "update_collaboration",
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
    "inspect_agent_tree",
    # 结论账只写本 run 自己的 findings.jsonl(内部记账面,不碰用户空间)——纯分析/
    # 研读任务正是"结论只活在对话里"的重灾区,读侧预设也要能记账。
    "record_finding",
]
_CODING_TOOL_PRESETS = {"coding"}


def subagent_allowed_tools(params: dict[str, object]) -> list[str] | None:
    allowed_tools = string_list(params.get("allowed_tools"), TOOL_TEXT_LIST_OPTIONS)
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


def _merge_tool_grants(explicit: list[str], baseline: list[str] | None) -> list[str]:
    return list(dict.fromkeys([*explicit, *(baseline or [])]))
