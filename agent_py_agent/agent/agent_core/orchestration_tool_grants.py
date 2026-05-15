# LLM: Subagent tool grant helpers keep role presets permissive by default.
# 模块用途: 集中处理子代理工具预设，避免角色模板被误变成没有读写能力的空代理。

from __future__ import annotations

from .parameters import _string_list

CODING_SUBAGENT_TOOLS = [
    "list_files",
    "read_file",
    "search_text",
    "read_artifact",
    "write_file",
    "append_file",
    "replace_in_file",
    "capability_request",
]
READ_ONLY_SUBAGENT_TOOLS = list(CODING_SUBAGENT_TOOLS)
_CODING_TOOL_PRESETS = {"coding", "frontend-dev", "frontend", "web", "web-dev", "file-edit", "edit"}


# LLM: subagent_allowed_tools resolves explicit grants without treating empty lists as no-tools.
# 函数用途: 处理 create_subagents 的 allowed_tools/tool_preset；省略或 none 都回到自动策略。
def subagent_allowed_tools(params: dict[str, object]) -> list[str] | None:
    allowed_tools = _string_list(params.get("allowed_tools"))
    preset = str(params.get("tool_preset") or "").strip().lower()
    preset_tools = _preset_allowed_tools(preset) if preset else None
    if allowed_tools:
        if preset_tools:
            return list(dict.fromkeys([*allowed_tools, *preset_tools]))
        return allowed_tools
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
