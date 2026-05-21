# LLM: Subagent tool grant helpers keep role presets permissive by default.
# 模块用途: 集中处理子代理工具预设，避免角色模板被误变成没有读写能力的空代理。

from __future__ import annotations

from .parameters import _string_list

CODING_SUBAGENT_TOOLS = [
    "list_files",
    "read_file",
    "search_text",
    "read_artifact",
    "fetch_url",
    "http_request",
    "write_file",
    "append_file",
    "replace_in_file",
    "file_write_session",
    "write_structured_json",
    "data_to_workbook",
    "markdown_to_pdf",
    "schedule_child_subagents",
    "dispatch_subagents",
    "subagent_board",
    "subagent_message",
    "capability_request",
]
READ_ONLY_SUBAGENT_TOOLS = list(CODING_SUBAGENT_TOOLS)
_CODING_TOOL_PRESETS = {
    "coding",
    "coder",
    "frontend-dev",
    "frontend",
    "web",
    "web-dev",
    "file-edit",
    "edit",
    "research",
    "researcher",
    "writer",
    "tester",
    "acceptor",
    "worker",
}


# LLM: subagent_allowed_tools resolves explicit grants without treating empty lists as no-tools.
# 函数用途: 处理 create_subagents 的 allowed_tools/tool_preset；模型少填工具时补齐基础读写，避免子代理被误限制。
def subagent_allowed_tools(params: dict[str, object]) -> list[str] | None:
    allowed_tools = _string_list(params.get("allowed_tools"))
    preset = str(params.get("tool_preset") or "").strip().lower()
    preset_tools = _preset_allowed_tools(preset) if preset else None
    if allowed_tools:
        return _merge_tool_grants(allowed_tools, preset_tools or CODING_SUBAGENT_TOOLS)
    if "tool_preset" not in params:
        return None
    return _preset_allowed_tools(preset or "read_only")


# LLM: _preset_allowed_tools maps semantic task presets to baseline-capable tool grants.
# 函数用途: 把 frontend-dev/coding 等模型常用预设转成稳定工具包；未知预设回退给角色模板自动判断。
def _preset_allowed_tools(preset: str) -> list[str] | None:
    if preset in _CODING_TOOL_PRESETS:
        return list(CODING_SUBAGENT_TOOLS)
    if preset == "read_only":
        return list(READ_ONLY_SUBAGENT_TOOLS)
    if preset == "none":
        return None
    return None


# LLM: _merge_tool_grants treats model-provided allowed_tools as hints, not hard capability removal.
# 函数用途: 合并显式工具和基础工具包，保留模型意图但不让少填列表砍掉 write_file/read_artifact。
def _merge_tool_grants(explicit: list[str], baseline: list[str] | None) -> list[str]:
    return list(dict.fromkeys([*explicit, *(baseline or [])]))
