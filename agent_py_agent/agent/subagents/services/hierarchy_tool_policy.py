# LLM: Tool policy helpers keep hierarchy scheduling from growing tool-selection branches inline.
# 模块用途: 判断 child/leaf/coordinator 应拿哪些工具，统一修正常见模型工具名别名。

from __future__ import annotations

"""Tool selection policy for scheduled hierarchy children."""

from dataclasses import dataclass
from typing import Any, ClassVar

from ..role_templates import COORDINATOR_TOOLS

_DEFAULT_LEAF_CODING_TOOLS = [
    "list_files",
    "read_file",
    "search_text",
    "write_file",
    "append_file",
    "replace_in_file",
]
_LEAF_ORCHESTRATION_TOOLS = {"schedule_child_subagents", "dispatch_subagents", "subagent_board"}
_TOOL_NAME_ALIASES = {
    "append": "append_file",
    "list": "list_files",
    "read": "read_file",
    "replace": "replace_in_file",
    "search": "search_text",
    "write": "write_file",
}
_WRITE_INTENT_MARKERS = (
    "write_file",
    "append_file",
    "replace_in_file",
    "写入",
    "写文件",
    "产物路径",
    "output path",
    "deliverable",
    ".py",
    ".md",
    ".json",
    ".txt",
    ".html",
    ".css",
    ".js",
)


# LLM: LeafWriteIntentRequest bundles the signals used to infer concrete file-writing intent.
# 类用途: 保存 child spec、授权写入根和当前 goal，避免写入意图判断继续扩散参数。
@dataclass(frozen=True)
class LeafWriteIntentRequest:
    """Inputs for deciding whether a scheduled child is a product-writing leaf."""

    __test__: ClassVar[bool] = False

    spec: Any
    extra_write_roots: list[str]
    goal: str | None = None


# LLM: ToolPolicyRequest bundles tool-selection inputs for one scheduled child run.
# 类用途: 保存父级工具、child spec、写入根和 goal，用于生成下一层 allowed_tools。
@dataclass(frozen=True)
class ToolPolicyRequest:
    """Inputs for deriving allowed tools for a scheduled child."""

    __test__: ClassVar[bool] = False

    parent_tools: list[str]
    spec: Any
    extra_write_roots: list[str]
    goal: str | None = None


# LLM: scheduled_child_tools applies coordinator and leaf policies without touching persistence.
# 函数用途: 根据 child 角色、显式工具和写产物意图，返回去重后的下一层工具列表；coordinator 显式工具也会补齐编排工具。
def scheduled_child_tools(request: ToolPolicyRequest) -> list[str]:
    """Return the allowed tools for a scheduled child."""

    should_infer_leaf_tools = should_infer_leaf_coding_tools(
        LeafWriteIntentRequest(
            spec=request.spec,
            extra_write_roots=request.extra_write_roots,
            goal=request.goal,
        )
    )
    if request.spec.allowed_tools:
        explicit_tools = [_canonical_tool_name(item) for item in request.spec.allowed_tools]
        if is_coordinator_spec(request.spec):
            return _coordinator_tools([*explicit_tools, *COORDINATOR_TOOLS])
        if should_infer_leaf_tools:
            return _leaf_write_tools([*explicit_tools, *_DEFAULT_LEAF_CODING_TOOLS])
        return list(dict.fromkeys(explicit_tools))
    if is_coordinator_spec(request.spec):
        return _coordinator_tools([*request.parent_tools, *COORDINATOR_TOOLS])
    if should_infer_leaf_tools:
        return _leaf_write_tools([*request.parent_tools, *_DEFAULT_LEAF_CODING_TOOLS])
    return request.parent_tools


# LLM: should_infer_leaf_coding_tools keeps automatic write-tool inference narrow and auditable.
# 函数用途: 只在非 coordinator child、继承写入根且文本里有明确写文件意图时返回 True。
def should_infer_leaf_coding_tools(request: LeafWriteIntentRequest) -> bool:
    if not request.extra_write_roots:
        return False
    if is_coordinator_spec(request.spec):
        return False
    checks = list(getattr(request.spec, "acceptance_checks", []) or [])
    intent_text = "\n".join([request.goal or request.spec.goal, *checks]).lower()
    return any(marker.lower() in intent_text for marker in _WRITE_INTENT_MARKERS)


# LLM: is_coordinator_spec centralizes role/name checks used by role and tool inference.
# 函数用途: 判断 child spec 是否明确是协调节点，避免 leaf 规则误伤 coordinator。
def is_coordinator_spec(spec: Any) -> bool:
    role_text = f"{getattr(spec, 'role', '')} {getattr(spec, 'agent_name', '')}".lower()
    return "coordinator" in role_text or "lead" in role_text


# LLM: _leaf_write_tools strips hierarchy orchestration grants from concrete product-writing leaves.
# 函数用途: 给 leaf 写文件任务保留文件读写工具，去掉继续派下级的工具。
def _leaf_write_tools(tools: list[str]) -> list[str]:
    return list(dict.fromkeys(tool for tool in tools if tool not in _LEAF_ORCHESTRATION_TOOLS))


# LLM: _coordinator_tools preserves report-writing tools but keeps this hook for orchestration policy.
# 函数用途: coordinator/lead 可写自己的计划/证据报告；业务产物仍交给 worker/writer。
def _coordinator_tools(tools: list[str]) -> list[str]:
    return list(dict.fromkeys(tools))


# LLM: _canonical_tool_name maps common model aliases to real ToolRegistry names before runner prompts see them.
# 函数用途: 把模型常写的 write/read/list 等口语化工具名转成真实工具名。
def _canonical_tool_name(tool_name: object) -> str:
    text = str(tool_name or "").strip()
    return _TOOL_NAME_ALIASES.get(text, text)
