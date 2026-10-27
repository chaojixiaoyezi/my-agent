# LLM: Bot权限只收窄宿主快照；工具名称和结构化操作均须命中正向集合，未知MCP不能凭只读自述放行。
# 模块用途: 共用双机器人模式归一化、操作准入和工具快照裁剪，不授予任何新工具。
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict, replace
from pathlib import Path
from types import SimpleNamespace

from ..plugin_host.errors import HostAPIError

BOT_MODES = frozenset({"query_readonly", "action_avatar"})
_BOT_MODE_ALIASES = {"ops_config": "action_avatar", "execution": "action_avatar", "config_rw": "action_avatar"}
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


# LLM: 别名仅限ops_config/execution/config_rw；两个参数归一后必须一致，不掩盖只读/执行模式冲突。
# 函数用途: 将页面模式变为宿主唯一Bot模式，缺省为只读查询。
def normalize_bot_mode(bot_mode=None, bot_kind=None):
    values = [_BOT_MODE_ALIASES.get(value, value)
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


# LLM: 策略不能自证Bot身份；核host owner、canonical线程/home/代次/禁学及metadata一致性，普通插件工具请求仍拒绝。
# LLM: 同时冷读原input receipt冻结的prepared_request策略，不能凭队列metadata自行扩充宿主签发的工具集。
# 函数用途: 验证queued Bot信封确实绑定当前worker自己的受保护Bot线程。
def verified_bot_request(agent, request, thread_id, policy):
    from ..audit_group.access_policy import owner_identity
    from ..common.nofollow_fs import read_bytes_beneath
    from ..common.opaque_id import validate_opaque_id
    from ..plugin_host.schemas import strict_json
    from ..user_space.owner_resolver import resolve_owner_home
    from ..user_space.runtime_paths import resolve_runtime_paths_for_agent
    if (request.get("source") != "plugin" or not isinstance(policy, Mapping)
            or policy.get("schema") != "plugin_execution_policy.v1" or policy.get("network") is not False
            or policy.get("bot_mode") not in BOT_MODES or policy.get("thread_id") != thread_id
            or type(policy.get("bot_generation")) is not int or type(policy.get("tool_names")) not in (list, tuple)
            or len(policy["tool_names"]) > 2000 or any(type(name) is not str for name in policy["tool_names"])):
        raise HostAPIError("BOT_TOOL_DENIED", 403)
    home = resolve_owner_home(Path(agent.config.my_agent_home).resolve(), owner_identity(request.get("owner_id")))
    thread = agent.conversation_store.threads.require(thread_id)
    paths = resolve_runtime_paths_for_agent(agent.config, home.home_dir,
        SimpleNamespace(owner_home_dir=home.home_dir, owner_workspace_dir=home.workspace_dir)).paths
    expected = paths["conversation_workspace"].resolve()
    if (request.get("host_request_owner.v1") != asdict(home.identity)
            or not expected.is_relative_to(home.home_dir) or agent.conversation_store.storage.root.resolve() != expected
            or thread.owner_id != home.owner_id or thread.owner_home != str(home.home_dir)):
        raise HostAPIError("BOT_TOOL_DENIED", 403)
    metadata = request.get("metadata") or {}
    for key in ("bot_mode", "bot_generation", "learning_excluded"):
        if key not in thread.metadata or policy.get(key) != thread.metadata[key] or metadata.get(key) != thread.metadata[key]:
            raise HostAPIError("BOT_TOOL_DENIED", 403)
    readonly = policy["bot_mode"] == "query_readonly"
    if policy.get("learning_excluded") is not readonly or policy.get("read_only_mode") is not readonly:
        raise HostAPIError("BOT_TOOL_DENIED", 403)
    validate_opaque_id(request.get("id"), kind="request")
    parts = (*paths["gateway_workspace"].resolve().relative_to(home.home_dir).parts,
             "input_receipts", request["id"] + ".json")
    receipt = strict_json(read_bytes_beneath(home.home_dir, parts, max_bytes=4 * 1024 * 1024), 4 * 1024 * 1024)
    prepared = receipt.get("prepared_request", {})
    if (receipt.get("schema_version") not in {"gateway_input_receipt.v2", "gateway_input_receipt.v3"}
            or receipt.get("request_id") != request["id"] or prepared.get("id") != request["id"]
            or prepared.get("owner_id") != home.owner_id or prepared.get("host_request_owner.v1") != asdict(home.identity)
            or prepared.get("conversation") != {"thread_id": thread_id}
            or prepared.get("metadata", {}).get("plugin_execution_policy") != policy):
        raise HostAPIError("BOT_TOOL_DENIED", 403)
    return home


# LLM: 准入信封只提供上界；worker现读enabled/治理/permissions/禁用工具及成员，再与实际运行快照求交，不改handler。
# 函数用途: 将已验证的Bot请求转换成RunParams工具白名单，撤权不能靠排队时快照绕过。
def bot_request_allowed_tools(agent, request, thread_id, policy):
    from ..audit_group.access_policy import current_tool_policy, require_audit_group_member
    from ..user_space.local_user_store import LocalUserStore
    from ..user_space.managed_user_governance import _GovernanceControl
    from ..user_space.owner_resolver import OwnerIdentity
    home = verified_bot_request(agent, request, thread_id, policy)
    users = LocalUserStore(home.root)
    if home.identity.provider == "local" and home.identity.owner_kind == "user":
        users.require_enabled(home.identity.owner_id)
        governance = _GovernanceControl(home.root, home.identity.owner_id).policy()
        if policy.get("governance_revision") != governance.get("revision", 0):
            raise HostAPIError("BOT_TOOL_DENIED", 403)
    current, controls = current_tool_policy(home.root, home)
    if controls.get("disabled", False):
        raise HostAPIError("BOT_TOOL_DENIED", 403)
    names = set(policy["tool_names"]) - set(current.get("disabled_tools", []))
    permissions = users.read_json((*home.home_dir.relative_to(users.root).parts, "permissions.json")) or {}
    if permissions.get("filesystem", {}).get("access_mode") == "read-only":
        names &= READ_TOOLS
    member = home.identity == OwnerIdentity.local_main()
    if names & (AUDIT_TOOLS | {"audit_group_invoke_asset"}):
        require_audit_group_member(home.root, home.identity)
        member = True
    restricted = snapshot_for_bot_request(agent.tools.runtime_snapshot(),
        {**policy, "tool_names": sorted(names), "in_audit_group": member})
    return sorted(restricted.allowed_tools)
