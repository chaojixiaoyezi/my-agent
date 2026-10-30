# LLM: 直属工具预设合并前先冻结显式旧目录的动作上界；record_lesson 与 capability_request 同属父子控制面，
#   实际可见仍经 active_model_subagent_tools、owner 禁用表与注册表快照收窄。改预设须同步工具常量与派工测试。
# 模块用途: 定义直属子代理的 coding/read_only 工具预设，并把显式授权与预设合并成 child 工具快照。
from __future__ import annotations

from ...common.value_parsing import TOOL_TEXT_LIST_OPTIONS, string_list
from ...subagents.role_templates import (
    AGENT_PROGRESS_TOOLS,
    OWNER_TASK_TOOLS,
    RECORD_LESSON_TOOL,
    SELF_DISCOVERY_TOOLS,
    SHELL_SESSION_TOOLS,
    active_model_subagent_tools,
)
from ...tooling.registry_auth import canonical_discovery_grants
from ...tooling.write_boundary import WRITE_TOOL_ORDER

CODING_SUBAGENT_TOOLS = [
    *SELF_DISCOVERY_TOOLS,
    *OWNER_TASK_TOOLS,
    *AGENT_PROGRESS_TOOLS,
    "list_files",
    "read_file",
    "search_text",
    "read_artifact",
    "skill_search",
    "web_search",
    "web_fetch",
    "watch_stream",
    *WRITE_TOOL_ORDER,
    *SHELL_SESSION_TOOLS,
    "capability_request",
    RECORD_LESSON_TOOL,
]
READ_ONLY_SUBAGENT_TOOLS = [
    *SELF_DISCOVERY_TOOLS,
    *AGENT_PROGRESS_TOOLS,
    "list_files",
    "read_file",
    "search_text",
    "read_artifact",
    "skill_search",
    "web_search",
    "web_fetch",
    # 只读限制约束的是业务工具；能力申请属于父子控制面，必须始终可达。
    "capability_request",
    # 经验账只写本 run 自己的 lessons.jsonl（内部记账面，不碰用户空间）；研读类任务同样会形成可复用做法。
    RECORD_LESSON_TOOL,
]
_CODING_TOOL_PRESETS = {"coding"}


# LLM: 先规范化显式发现grant再合并预设；目录令牌不得被预设补入的tool_search覆盖，精确Audit仍取宿主scope，无IO。
# 函数用途: 按真实显式意图和预设生成子代理授权，保持退休过滤及只读目录限制。
def subagent_allowed_tools(params: dict[str, object]) -> list[str] | None:
    allowed_tools = canonical_discovery_grants(active_model_subagent_tools(
        string_list(params.get("allowed_tools"), TOOL_TEXT_LIST_OPTIONS)
    ))
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
