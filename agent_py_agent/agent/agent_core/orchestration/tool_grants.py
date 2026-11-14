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
    template_for_role,
)
from ...tooling.registry_auth import action_ceilings, normalize_tool_grants
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
    explicit_list = string_list(params.get("allowed_tools"), TOOL_TEXT_LIST_OPTIONS)
    allowed_tools = normalize_tool_grants(active_model_subagent_tools(explicit_list))
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
            return normalize_tool_grants(active_model_subagent_tools(exact_scope))
    # 窄角色不经 coding 预设补入 shell/写入；显式空清单必须原样保持。
    template = template_for_role(str(params.get("role") or ""))
    if template is not None and not template.inherit_base_tools:
        return allowed_tools if "allowed_tools" in params else normalize_tool_grants(list(template.default_tools))
    if explicit_list and not allowed_tools:
        return []
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


# LLM: 显式授权与预设分别归一；预设补入同名裸工具时必须保留显式 tool:action 动作上界，不扩权。
# 函数用途: 合并显式与基础工具，去重、删除已退休控制工具并保留精确动作上界。
def _merge_tool_grants(explicit: list[str], baseline: list[str] | None) -> list[str]:
    exp = normalize_tool_grants(active_model_subagent_tools(explicit))
    base = normalize_tool_grants(active_model_subagent_tools(baseline or []))
    exp_bare = {item for item in exp if ":" not in item}
    exp_ceil = action_ceilings(exp)
    base_ceil = action_ceilings(base)
    bare_order = [
        *(item for item in exp if ":" not in item),
        *(item for item in base if ":" not in item and item not in exp_bare),
    ]
    tokens: list[str] = []
    for name in bare_order:
        acts = exp_ceil.get(name) if name in exp_bare else base_ceil.get(name)
        if acts:
            tokens.extend(f"{name}:{act}" for act in sorted(acts))
    return [*bare_order, *tokens]
