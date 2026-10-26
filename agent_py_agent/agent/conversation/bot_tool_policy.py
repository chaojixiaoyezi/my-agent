# LLM: Bot权限只收窄宿主快照；工具名称和结构化操作均须命中正向集合，未知MCP不能凭只读自述放行。
# 模块用途: 共用双机器人模式归一化、操作准入和工具快照裁剪，不授予任何新工具。
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace

from ..plugin_host.errors import HostAPIError

BOT_MODES = frozenset({"query_readonly", "action_avatar"})
READ_TOOLS = frozenset({
    "read_file", "list_files", "find_files", "search_text", "read_artifact", "web_search",
    "memory_search", "session_search", "self_map", "list_tools", "get_goal", "list_agents",
    "get_session_task", "audit_records", "audit_group_owners_list", "audit_group_assets_summary",
    "audit_group_alerts_list", "audit_group_posture_overview", "audit_group_query",
})
READ_OPERATIONS = {
    "remember": ("action", frozenset({"list"}), None),
    "user_config": ("action", frozenset({"get", "list"}), None),
    "scripts": ("action", frozenset({"list", "get", "status"}), None),
    "workflows": ("action", frozenset({"list", "get", "status"}), None),
    "schedule": ("action", frozenset({"list", "get"}), None),
    "process_session": ("action", frozenset({"list", "status", "network_status"}), None),
    "task_progress": ("action", frozenset({"read"}), "read"),
    "skill_search": ("action", frozenset({"get", "search"}), "search"),
}
AUDIT_TOOLS = frozenset(name for name in READ_TOOLS if name.startswith("audit_group_"))


# LLM: 别名只限任务明确的ops_config；两个参数同时存在必须一致，不能用别名掩盖模式冲突。
# 函数用途: 将页面模式变为宿主唯一Bot模式，缺省为只读查询。
def normalize_bot_mode(bot_mode=None, bot_kind=None):
    values = ["action_avatar" if value == "ops_config" else value
              for value in (bot_mode, bot_kind) if value is not None]
    if any(value not in BOT_MODES for value in values) or len(set(values)) > 1:
        raise HostAPIError("BOT_MODE_MISMATCH", 409)
    return values[0] if values else "query_readonly"


# LLM: action_avatar仍受已核owner许可和当前成员资格限制；query只认正向工具/操作，绝不按名称后缀猜只读。
# 函数用途: 判断一次具体工具调用是否符合Bot模式及账号权限上界。
def bot_tool_allowed(bot_mode, tool_name, arguments=None, *, allowed_tools, in_audit_group=False):
    mode = normalize_bot_mode(bot_mode)
    if tool_name not in allowed_tools:
        return False
    if tool_name.startswith("audit_group_") and not in_audit_group:
        return False
    if mode == "action_avatar":
        return True
    if tool_name in READ_TOOLS:
        return True
    rule = READ_OPERATIONS.get(tool_name)
    if rule is None or not isinstance(arguments, Mapping):
        return False
    parameter, values, default = rule
    operation = arguments.get(parameter, default)
    return isinstance(operation, str) and operation in values


# LLM: 这是执行前硬门；调用者必须在handler前使用，不能用目录过滤替代操作级验证。
# 函数用途: 对禁止的Bot工具调用返回稳定403。
def require_bot_tool(bot_mode, tool_name, arguments, *, allowed_tools, in_audit_group=False):
    if not bot_tool_allowed(bot_mode, tool_name, arguments, allowed_tools=allowed_tools,
                            in_audit_group=in_audit_group):
        raise HostAPIError("BOT_TOOL_DENIED", 403)


# LLM: 混合读写工具必须另有逐操作硬门，快照默认剥离；不修改handler、原权限或发现结果来伪造授权。
# 函数用途: 返回只含当前成员和owner许可交集的工具快照。
def build_bot_snapshot(snapshot, bot_mode, *, allowed_tools, in_audit_group=False):
    mode = normalize_bot_mode(bot_mode)
    parent = snapshot.available_tool_names if snapshot.allowed_tools is None else snapshot.allowed_tools
    names = frozenset(allowed_tools) & parent
    if mode == "query_readonly":
        names &= READ_TOOLS
    if not in_audit_group:
        names -= AUDIT_TOOLS | {"audit_group_invoke_asset"}
    runtimes = tuple(row for row in snapshot.runtimes if row.model_spec.name in names
                     and row.availability.available and row.exposure.model_visible)
    names = frozenset(row.model_spec.name for row in runtimes)
    return replace(snapshot, runtimes=runtimes, available_tool_names=names, allowed_tools=names,
                   unavailable_tools=(), snapshot_hash="", presentation_deferred_names=None,
                   presentation_shortlist_names=None, loaded_tool_names=(), preloaded_tool_names=(),
                   catalog_specs=())


# LLM: 只消费宿主已核账号/治理后的执行信封，仍与实际owner工具相交，不由元数据创造handler。
# 函数用途: 将Bot请求策略应用到当前真实工具快照。
def snapshot_for_bot_request(snapshot, policy):
    if (not isinstance(policy, Mapping) or policy.get("schema") != "plugin_execution_policy.v1"
            or type(policy.get("tool_names")) not in (list, tuple)):
        raise HostAPIError("BOT_TOOL_DENIED", 403)
    return build_bot_snapshot(snapshot, policy.get("bot_mode"), allowed_tools=policy["tool_names"],
                              in_audit_group=policy.get("in_audit_group") is True)
