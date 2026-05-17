# LLM: Tool policy helpers keep hierarchy scheduling from growing tool-selection branches inline.
# 模块用途: 判断 child/leaf/coordinator 应拿哪些工具，统一修正工具名别名；不从自然语言 goal 猜写入意图。

from __future__ import annotations

"""Tool selection policy for scheduled hierarchy children."""

from dataclasses import dataclass
from typing import Any, ClassVar

from ..role_templates import COORDINATOR_TOOLS

# LLM: Leaf defaults mirror main-agent basics: read/write/search/web plus orchestration, with no role-specific handcuffs.
# 函数用途: 给调度出来的小傻妞默认补齐读写、网页证据和继续派工能力，减少因模板或自然语言误差导致的“有任务但没工具”。
_DEFAULT_LEAF_CODING_TOOLS = [
    "list_files",
    "read_file",
    "search_text",
    "read_artifact",
    "fetch_url",
    "http_request",
    "write_file",
    "append_file",
    "replace_in_file",
    "schedule_child_subagents",
    "dispatch_subagents",
    "subagent_board",
    "subagent_message",
    "capability_request",
]
# LLM: Leaf defaults intentionally include coordination tools; role changes prompt style, not basic capability.
_TOOL_NAME_ALIASES = {
    "append": "append_file",
    "list": "list_files",
    "read": "read_file",
    "replace": "replace_in_file",
    "search": "search_text",
    "write": "write_file",
}
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

    if request.spec.allowed_tools:
        explicit_tools = [_canonical_tool_name(item) for item in request.spec.allowed_tools]
        if is_coordinator_spec(request.spec):
            return _coordinator_tools([*explicit_tools, *_DEFAULT_LEAF_CODING_TOOLS, *COORDINATOR_TOOLS])
        return _leaf_write_tools([*explicit_tools, *_DEFAULT_LEAF_CODING_TOOLS])
    if is_coordinator_spec(request.spec):
        return _coordinator_tools([*request.parent_tools, *_DEFAULT_LEAF_CODING_TOOLS, *COORDINATOR_TOOLS])
    return _leaf_write_tools([*request.parent_tools, *_DEFAULT_LEAF_CODING_TOOLS])


# LLM: should_infer_leaf_coding_tools keeps automatic write-tool inference structural and auditable.
# 函数用途: 只在非 coordinator child 且继承写入根时返回 True；不解析“写/生成/产物”等自然语言。
def should_infer_leaf_coding_tools(request: LeafWriteIntentRequest) -> bool:
    if not request.extra_write_roots:
        return False
    if is_coordinator_spec(request.spec):
        return False
    return True


# LLM: is_coordinator_spec centralizes role/name checks used by role and tool inference.
# 函数用途: 判断 child spec 是否明确是协调节点，避免 leaf 规则误伤 coordinator。
def is_coordinator_spec(spec: Any) -> bool:
    role_text = f"{getattr(spec, 'role', '')} {getattr(spec, 'agent_name', '')}".lower()
    return "coordinator" in role_text or "lead" in role_text


# LLM: _leaf_write_tools preserves baseline read/write/orchestration tools for capable small agents.
# 函数用途: 给 leaf 写文件任务保留完整基础能力；是否继续派下级由任务和模型判断，不由工具层硬砍。
def _leaf_write_tools(tools: list[str]) -> list[str]:
    return list(dict.fromkeys(tools))


# LLM: _coordinator_tools preserves all inherited tools and adds orchestration grants.
# 函数用途: coordinator/lead 保留基础读写和协调工具；是否亲自写产物由任务上下文决定，不由角色硬限制。
def _coordinator_tools(tools: list[str]) -> list[str]:
    return list(dict.fromkeys(tools))


# LLM: _canonical_tool_name maps common model aliases to real ToolRegistry names before runner prompts see them.
# 函数用途: 把模型常写的 write/read/list 等口语化工具名转成真实工具名。
def _canonical_tool_name(tool_name: object) -> str:
    text = str(tool_name or "").strip()
    return _TOOL_NAME_ALIASES.get(text, text)
