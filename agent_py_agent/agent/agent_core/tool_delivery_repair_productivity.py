# LLM: Delivery repair productivity rules are shared by the staged repair guard.
# 模块用途: 根据结构化 closeout recovery_actions 判断哪些工具调用算真实推进，哪些只是检查空转。

from __future__ import annotations

from dataclasses import dataclass

PRODUCTIVE_TOOL_NAMES = {
    "append_file",
    "api_json_collection",
    "data_to_workbook",
    "file_write_session",
    "markdown_to_pdf",
    "replace_in_file",
    "run_command",
    "write_structured_json",
    "write_file",
}
INSPECTION_ONLY_TOOL_NAMES = {
    "list_files",
    "list_tools",
    "read_file",
}
EVIDENCE_GATHERING_TOOL_NAMES = {
    "fetch_url",
    "http_request",
    "read_artifact",
    "search",
}
STRICT_REPAIR_PRODUCTIVE_TOOLS = {
    "__parse_error__",
    "append_file",
    "api_json_collection",
    "data_to_workbook",
    "file_write_session",
    "markdown_to_pdf",
    "replace_in_file",
    "write_structured_json",
    "write_file",
}


# LLM: DeliveryRepairProductivityContext stores structured runtime facts for contract decisions.
# 类用途: 保存当前模块使用的结构化字段，避免后续流程从普通自然语言推断机器事实。
@dataclass(frozen=True)
class DeliveryRepairProductivityContext:
    productive_tools: set[str]
    inspection_only_tools: set[str]
    strict_write_required: bool
    required_actions: list[dict[str, object]]


# LLM: productive_tools decides which tools count as progress for the current staged-repair phase.
# 函数用途: 根据 strict_write_required 和 builder_tool 集合计算本轮允许视为“有效修复推进”的工具名集合。
def productive_tools(payload: dict[str, object], action_tools: set[str]) -> set[str]:
    declared_tools = {tool for tool in action_tools if tool}
    if bool(payload.get("strict_write_required")):
        return declared_tools | STRICT_REPAIR_PRODUCTIVE_TOOLS
    return declared_tools | PRODUCTIVE_TOOL_NAMES


# LLM: inspection_only_tools tightens the redirect rule only when the contract has advanced to a builder-ready stage.
# 函数用途: 基础检查工具一直算 inspection-only；只有进入 invoke_builder_tool 阶段时，read_artifact 才一并视为拖延动作。
def inspection_only_tools(required_actions: list[dict[str, object]], strict_write_required: bool) -> set[str]:
    tools = set(INSPECTION_ONLY_TOOL_NAMES)
    if not strict_write_required and _has_artifact_finding_repair_action(required_actions):
        tools.difference_update(_allowed_artifact_repair_inspection_tools(required_actions))
    if not strict_write_required and not _has_builder_ready_action(required_actions):
        return tools
    tools.update(EVIDENCE_GATHERING_TOOL_NAMES)
    if strict_write_required or _has_builder_ready_action(required_actions):
        tools.add("read_artifact")
    return tools


# LLM: Data-gathering tools stay available while checkpoints need facts, but not once a builder is ready.
# 函数用途: 判断当前修复动作是否已经进入 builder 阶段；builder ready 后继续读证据就是拖延，应回到构建工具。
def _has_builder_ready_action(required_actions: list[dict[str, object]]) -> bool:
    return any(str(item.get("recommended_action") or "") == "invoke_builder_tool" for item in required_actions)


# LLM: _has_artifact_finding_repair_action checks artifact-finding repair actions from structured fields.
# 函数用途: 只读取 recommended_action，不解析 finding 的自然语言说明。
def _has_artifact_finding_repair_action(required_actions: list[dict[str, object]]) -> bool:
    return any(str(item.get("recommended_action") or "") == "repair_artifact_against_findings" for item in required_actions)


# LLM: Artifact repair only treats inspection as progress when the artifact is still missing.
# 函数用途: 已存在但验收失败的产物需要写入/替换推进；重复 read_file 不再算修复动作。
def _allowed_artifact_repair_inspection_tools(required_actions: list[dict[str, object]]) -> set[str]:
    codes = {
        str(code)
        for action in required_actions
        if str(action.get("recommended_action") or "") == "repair_artifact_against_findings"
        for code in action.get("finding_codes", [])
        if isinstance(action.get("finding_codes"), list)
    }
    if codes and codes.issubset({"ARTIFACT_MISSING"}):
        return {"list_files"}
    return set()


__all__ = [
    "DeliveryRepairProductivityContext",
    "EVIDENCE_GATHERING_TOOL_NAMES",
    "inspection_only_tools",
    "productive_tools",
]
