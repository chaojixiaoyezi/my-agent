# LLM: 本模块是宿主指定版本的输入边界；模型参数不得选择版本，规范handler不保留旧形态分支。
# 模块用途: 将未执行的旧工具请求转换为规范输入，不重执行完成历史。
from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from dataclasses import replace
from types import MappingProxyType
from typing import Any

from ..contracts.error_taxonomy import error_contract


# LLM: sol2分类表未集成时仅补这个已约定的参数错误；其余未知码保持原UNKNOWN_ERROR，禁止注册或热改全局表。
# 函数用途: 为工具结果提供可删除的本地错误分类兼容读口。
def canonical_error_contract(code):
    contract = error_contract(code)
    if code == "TOOL_SCHEMA_INVALID_MODE_COMBINATION" and contract.code == "UNKNOWN_ERROR":
        return replace(error_contract("TOOL_INVALID_ARGUMENTS"), code=code)
    return contract


# LLM: 版本只取宿主协议而非模型字段；本函数纯转换，不授权、不执行或重放历史；新输入原样交现行schema校验。
# 函数用途: 将显式v1请求转换为唯一规范工具名和独立参数副本。
def decode_tool_input(name, params, *, version=2):
    if type(version) is not int or version not in {1, 2} or not isinstance(params, dict):
        raise ValueError("无效工具输入协议")
    values = deepcopy(params)
    if version == 2:
        return name, values
    if name == "list_tools":
        if values:
            raise ValueError("旧目录调用不接受参数")
        return "tool_search", {"action": "catalog"}
    if name == "skill_feedback":
        if set(values) - {"operation", "action", "skill_id"}:
            raise ValueError("未知反馈参数")
        if "operation" in values:
            operation = values.pop("operation")
            if operation not in {"status", "adopted"} or ("action" in values and values["action"] != operation):
                raise ValueError("反馈动作冲突")
            values["action"] = operation
    elif name == "task_progress" and values.get("action") == "create":
        values["action"] = "update"
    elif name == "session_search":
        values = _session_v1(values)
    elif name == "create_subagents":
        values = _create_v1(values)
    return name, values


# LLM: v1仍先核原字段类型和范围，再沿真实优先级选有效字段；显式mode不得混进旧语法。
# 函数用途: 将旧会话检索的六种隐式形态转换为规范mode。
def _session_v1(values):
    from ..capability.session_search_tool import SESSION_SEARCH_MODES

    strings = {"query", "around_id", "source_type", "message_id", "thread_id"}
    bounds = {"limit": (1, 20), "window": (1, 20), "offset": (0, None),
        "max_chars": (200, 20000), "cursor": (0, None)}
    if set(values) - strings - set(bounds) - {"current_thread"}:
        raise ValueError("旧会话请求含未知字段或混入mode")
    for key, value in values.items():
        if key in strings and not isinstance(value, str):
            raise ValueError("旧会话字符串字段类型错误")
        if key == "current_thread" and type(value) is not bool:
            raise ValueError("current_thread必须是布尔值")
        if key in bounds:
            low, high = bounds[key]
            if type(value) is not int or value < low or (high is not None and value > high):
                raise ValueError("旧会话数值字段越界")
    if values.get("message_id", "").strip():
        mode = "message"
    elif values.get("around_id", "").strip():
        mode = "around"
    elif values.get("current_thread") is True:
        mode = "thread_search" if values.get("query", "").strip() else "thread_browse"
    else:
        mode = "search" if values.get("query", "").strip() else "recent"
    return {"mode": mode, **{key: value for key, value in values.items() if key in SESSION_SEARCH_MODES[mode][1]}}


# 旧批次父级交付物/计划/说明/covers从未广播，不得借归一化改变孩子边界。
_V1_NON_BROADCAST = frozenset({"items", "goal", "description", "plan", "context_manifest", "covers",
    "output_files", "output_refs", "artifact_refs", "required_output_files", "required_output_refs",
    "deliverables", "final_output", "final_output_path"})
# 冻结的v1协议字段，不依赖上层agent_core或后续v2模型schema的演化。
_CREATE_V1_FIELDS = frozenset({"goal", "persistent_goal", "covers", "description", "role", "agent_name", "model",
    "effort", "tool_preset", "allowed_tools", "allowed_skills", "plan", "input_refs", "output_files", "artifact_refs",
    "replacement_for_run_ids", "related_finding_id", "long_running", "service_window_seconds", "audit_source_id", "input_media_refs"})


# LLM: 精确保留旧空items回单项与item显式空列表覆盖；模型不携内部标记，原显式字段事实由创建服务保存。
# 函数用途: 在输入边界展开旧顶层默认值，规范业务只消费独立items。
def _create_v1(values):
    allowed = _CREATE_V1_FIELDS | {"items"}
    if set(values) - allowed:
        raise ValueError("未知旧派工字段")
    items = values.get("items")
    if items is not None and not isinstance(items, list):
        raise ValueError("items必须是数组")
    if not items:
        item = {key: value for key, value in values.items() if key != "items"}
        if not isinstance(item.get("goal"), str) or not item["goal"].strip():
            raise ValueError("派工缺少非空goal")
        return {"items": [item]}
    defaults = {key: value for key, value in values.items() if key not in _V1_NON_BROADCAST}
    output = []
    for item in items:
        if (not isinstance(item, dict) or set(item) - _CREATE_V1_FIELDS or not isinstance(item.get("goal"), str)
                or not item["goal"].strip()):
            raise ValueError("每个item必须自带非空goal且字段合法")
        output.append({**defaults, **item})
    summary = values.get("goal")
    return {**({"batch_summary": summary} if summary else {}), "items": output}


# 冻结的 105 个业务动作到目标工具映射（40 space + 26 execution + 38 activity + 1 独立人工确认工具）。
_BUSINESS_DOMAIN_ACTIONS: Mapping[str, tuple[str, ...]] = MappingProxyType({
    "business_space": (
        "approvals.decide",
        "approvals.get",
        "approvals.list",
        "assets.get",
        "assets.list",
        "assets.register",
        "assets.unlink",
        "assets.versions.add",
        "assets.versions.list",
        "controllers.ensure",
        "controllers.get",
        "interfaces.bind",
        "interfaces.get",
        "interfaces.list",
        "interfaces.resolve",
        "interfaces.unbind",
        "interfaces.update",
        "model_profiles.list",
        "permissions.get",
        "policies.apply",
        "policies.get",
        "policies.preview",
        "sessions.space.get",
        "sessions.space.set",
        "spaces.archive",
        "spaces.capabilities.resolve",
        "spaces.create",
        "spaces.get",
        "spaces.list",
        "spaces.packages.attach",
        "spaces.packages.catalog",
        "spaces.packages.detach",
        "spaces.packages.list",
        "spaces.packages.preview",
        "spaces.packages.reorder",
        "spaces.packages.set",
        "spaces.restore",
        "spaces.update",
        "spaces.usage.list",
        "system.capabilities",
    ),
    "business_execution": (
        "agent_tree.get",
        "background_agents.create",
        "background_agents.get",
        "background_agents.list",
        "background_agents.messages.send",
        "background_agents.model.get",
        "background_agents.model.set",
        "background_agents.restore",
        "background_agents.resume",
        "background_agents.retire",
        "background_agents.suspend",
        "background_agents.update",
        "executions.cancel",
        "executions.get",
        "executions.list",
        "messages.list",
        "subagents.cancel",
        "subagents.get",
        "subagents.list",
        "subagents.messages.send",
        "tasks.assign",
        "tasks.cancel",
        "tasks.follow_up",
        "tasks.get",
        "tasks.list",
        "tasks.submit",
    ),
    "business_activity": (
        "artifacts.get",
        "artifacts.list",
        "changes.get",
        "events.list",
        "logs.read",
        "operations.get",
        "subscriptions.create",
        "subscriptions.get",
        "subscriptions.list",
        "subscriptions.pause",
        "subscriptions.remove",
        "subscriptions.resume",
        "subscriptions.update",
        "tool_calls.get",
        "tool_calls.list",
        "usage.get",
        "workflow_runs.cancel",
        "workflow_runs.get",
        "workflow_runs.list",
        "workflow_runs.resume",
        "workflows.cleanup.policy.get",
        "workflows.cleanup.policy.set",
        "workflows.cleanup.preview",
        "workflows.cleanup.run",
        "workflows.create",
        "workflows.disable",
        "workflows.enable",
        "workflows.get",
        "workflows.health.get",
        "workflows.list",
        "workflows.preview",
        "workflows.protection.set",
        "workflows.recycle",
        "workflows.restore",
        "workflows.run",
        "workflows.scope.get",
        "workflows.scope.set",
        "workflows.update",
    ),
    "business_assets_versions_accept": (
        "assets.versions.accept",
    ),
})

_BUSINESS_ACTION_TO_TOOL: Mapping[str, str] = MappingProxyType({
    action: target
    for target, actions in _BUSINESS_DOMAIN_ACTIONS.items()
    for action in actions
})

_RETIRED_TOOL_ALIASES: Mapping[str, tuple[str, str]] = MappingProxyType({
    f"business_{action.replace('.', '_')}": (target, action)
    for target, actions in _BUSINESS_DOMAIN_ACTIONS.items()
    if target != "business_assets_versions_accept"
    for action in actions
})


def business_domain_actions() -> Mapping[str, tuple[str, ...]]:
    """返回冻结的业务域动作全集映射。"""
    return _BUSINESS_DOMAIN_ACTIONS


def business_domain_tool(action: str) -> str:
    """按精确冻结表返回业务动作归属的规范工具名，不做近似匹配或自然语言推断。"""
    if not isinstance(action, str):
        raise ValueError("业务动作必须是字符串")
    target = _BUSINESS_ACTION_TO_TOOL.get(action)
    if target is None:
        raise ValueError(f"未知业务动作: {action}")
    return target


def retired_tool_aliases() -> Mapping[str, tuple[str, str]]:
    """返回不可变的 104 个退役业务叶子工具别名映射：legacy_name -> (target, action)。"""
    return _RETIRED_TOOL_ALIASES


def decode_retired_tool_call(
    name: str,
    arguments: dict[str, Any],
    *,
    available: frozenset[str],
) -> tuple[str, dict[str, Any], str] | None:
    """仅当原名称不在当前已注册可用集合时，按显式退役表转换未执行调用。"""
    if not isinstance(name, str) or not isinstance(arguments, dict) or not isinstance(available, (frozenset, set)):
        raise ValueError("无效退役工具调用输入")
    if name in available:
        return None
    alias = _RETIRED_TOOL_ALIASES.get(name)
    if alias is None:
        return None
    target, expected_action = alias
    if "action" in arguments:
        supplied = arguments["action"]
        if not isinstance(supplied, str) or supplied != expected_action:
            raise ValueError(f"退役工具 {name} 动作冲突: 期望 {expected_action}")
    decoded = deepcopy(arguments)
    decoded["action"] = expected_action
    return target, decoded, expected_action


def dead_business_spaces_redirect(arguments: object) -> dict[str, str]:
    """为已退役的旧聚合工具 business_spaces 生成不执行的结构化迁移指引。"""
    raw_action = arguments.get("action") if isinstance(arguments, dict) else None
    action = raw_action.strip() if isinstance(raw_action, str) else ""
    target = _BUSINESS_ACTION_TO_TOOL.get(action) if action else None
    if target is not None:
        return {
            "retired_tool": "business_spaces",
            "action": action,
            "target_tool": target,
            "next_action": (
                f"business_spaces 已退役且与现行操作回执身份不兼容，本次未执行；"
                f"请先用 tool_search 加载 {target}，再以 {target}(action=\"{action}\") 发起新调用。"
            ),
        }
    return {
        "retired_tool": "business_spaces",
        "target_tools": "business_space,business_execution,business_activity,business_assets_versions_accept",
        "next_action": (
            "business_spaces 已退役且本次未执行；请按动作改用 business_space、"
            "business_execution、business_activity 或独立工具 business_assets_versions_accept，"
            "调用前先通过 tool_search 加载目标工具。"
        ),
    }
