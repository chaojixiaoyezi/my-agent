# LLM: Bot权限只收窄宿主快照；离线能力须正向核验，执行器前共核目录、操作与请求总预算，不热改共享Agent。
# 模块用途: 共用双机器人模式归一化、操作准入和工具快照裁剪，不授予任何新工具。
from __future__ import annotations

from collections.abc import Mapping
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import asdict, replace
from pathlib import Path
from threading import Lock
from types import SimpleNamespace

from ..plugin_host.errors import HostAPIError
from ..tooling.registry import ToolRegistry

BOT_MODES = frozenset({"query_readonly", "action_avatar"})
_BOT_MODE_ALIASES = {"ops_config": "action_avatar", "execution": "action_avatar", "config_rw": "action_avatar"}
READ_TOOLS = frozenset({
    "read_file", "list_files", "find_files", "search_text", "read_artifact",
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
# 混合读写/执行工具不能整名认证离线：scripts/workflows等不进入快照；操作级只读判定仅兼容独立旧读口。
OFFLINE_TOOLS = READ_TOOLS | {"skill_search", "write_file", "edit_file", "apply_patch", "audit_group_invoke_asset"}


# LLM: 别名仅限ops_config/execution/config_rw；两个参数归一后必须一致，不掩盖只读/执行模式冲突。
# 函数用途: 将页面模式变为宿主唯一Bot模式，缺省为只读查询。
def normalize_bot_mode(bot_mode=None, bot_kind=None):
    values = [_BOT_MODE_ALIASES.get(value, value)
              for value in (bot_mode, bot_kind) if value is not None]
    if any(value not in BOT_MODES for value in values) or len(set(values)) > 1:
        raise HostAPIError("BOT_MODE_MISMATCH", 409)
    return values[0] if values else "query_readonly"


# LLM: action_avatar仍受owner/成员上界；显式离线策略只认已核工具，query旧操作读口不授予worker混合工具执行权。
# 函数用途: 判断一次具体工具调用是否符合Bot模式及账号权限上界。
def bot_tool_allowed(bot_mode, tool_name, arguments=None, *, allowed_tools, in_audit_group=False, network=None):
    mode = normalize_bot_mode(bot_mode)
    if tool_name not in allowed_tools:
        return False
    if network is False and tool_name not in OFFLINE_TOOLS:
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


# LLM: 混合读写工具仍另有逐操作硬门；离线工具须正向认证，过滤只收窄同一快照且保留原handler。
# 函数用途: 返回只含当前成员和owner许可交集的工具快照。
def build_bot_snapshot(snapshot, bot_mode, *, allowed_tools, in_audit_group=False, network=None):
    mode = normalize_bot_mode(bot_mode)
    parent = snapshot.available_tool_names if snapshot.allowed_tools is None else snapshot.allowed_tools
    names = frozenset(allowed_tools) & parent
    if network is False or mode == "query_readonly":
        names &= OFFLINE_TOOLS
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


# LLM: 消费宿主已核执行信封并与实际工具相交；network=False同时剥离网络及未经认证的离线扩展。
# 函数用途: 将Bot请求策略应用到当前真实工具快照。
def snapshot_for_bot_request(snapshot, policy):
    if (not isinstance(policy, Mapping) or policy.get("schema") != "plugin_execution_policy.v1"
            or type(policy.get("tool_names")) not in (list, tuple)):
        raise HostAPIError("BOT_TOOL_DENIED", 403)
    return build_bot_snapshot(snapshot, policy.get("bot_mode"), allowed_tools=policy["tool_names"],
                              in_audit_group=policy.get("in_audit_group") is True, network=policy.get("network", False))


# LLM: 只从已核OwnerHome和grant预算生成约束；目录必须仍在同一owner内，不从页面路径或模型attrs授权。
# 函数用途: 生成投递和worker共用的Bot网络、目录、调用上限与禁学策略。
def bot_execution_constraints(home, max_tool_calls, learning_excluded):
    root = home.workspace_dir.resolve()
    if (type(max_tool_calls) is not int or not 0 <= max_tool_calls <= 1000
            or type(learning_excluded) is not bool or not root.is_relative_to(home.home_dir.resolve())):
        raise HostAPIError("BOT_TOOL_DENIED", 403)
    return {"network": False, "workspace_root": str(root), "max_tool_calls": max_tool_calls,
            "learning_excluded": learning_excluded}


# LLM: 策略不能自证Bot身份；核host owner、canonical线程/home/代次/禁学及metadata一致性，普通插件工具请求仍拒绝。
# LLM: 同时冷读原input receipt冻结的完整策略并校验执行约束，不能凭队列metadata扩工具、目录或预算。
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
    constraints = bot_execution_constraints(home, (policy.get("budgets") or {}).get("max_tool_calls"), readonly)
    if policy.get("bot_execution_policy") != constraints:
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


# LLM: 每请求独享；锁覆盖额度判定和消耗，Compact沿同一实例；恢复仅取原canonical工具记录数，不以轮数估算。
# 类用途: 在原工具handler前限制Bot操作与总调用数，禁止并行调用超领。
class BotExecutionGuard:
    # LLM: policy来自worker完成receipt校验后的宿主参数，立即私有复制，不接受模型后续attrs修改。
    # 函数用途: 冻结本次Bot策略及已消费的调用次数。
    def __init__(self, policy, carried_calls=()):
        self.policy = deepcopy(policy)
        self.used, self.lock = len(carried_calls), Lock()

    # LLM: 只返回规范拒绝结果，调用方交回原executor落审计；不执行handler，不自行写操作账或修改权限。
    # 函数用途: 在handler之前按模式、离线集合和请求剩余额度放行一次调用。
    def check(self, call):
        from ..tooling.models import ToolHandlerOutcome

        policy = self.policy
        if not bot_tool_allowed(policy["bot_mode"], call.tool_name, call.arguments,
                allowed_tools=policy["tool_names"], in_audit_group=policy.get("in_audit_group") is True,
                network=policy["bot_execution_policy"]["network"]):
            return ToolHandlerOutcome(call.tool_name, False, "Bot工具策略拒绝本次操作。", error_code="BOT_TOOL_DENIED")
        with self.lock:
            if self.used >= policy["bot_execution_policy"]["max_tool_calls"]:
                return ToolHandlerOutcome(call.tool_name, False, "Bot本次请求的工具调用预算已耗尽。",
                    error_code="BOT_TOOL_BUDGET_EXCEEDED")
            self.used += 1
        return None


# LLM: 只克隆注册表视图，原handler/操作账/审批对象保留；原executor执行边界不能被模型工具名或attrs替换。
# 类用途: 为一条Bot请求提供目录围栏和执行前预算门，不改变普通会话注册表。
class _BotToolRegistry(ToolRegistry):
    # LLM: 跳过父构造及连接副作用；当前宿主注册表已初始化，只复制非权威视图并收窄工具与物理目录。
    # 函数用途: 创建请求私有的受限工具视图，保持底层原有审批和账本。
    def __init__(self, parent, guard):
        self.__dict__.update(parent.__dict__)
        self._bot_guard = guard
        self.tools = {name: tool for name, tool in parent.tools.items() if name in guard.policy["tool_names"]
                      and name in OFFLINE_TOOLS}
        self.workspace_root = Path(guard.policy["bot_execution_policy"]["workspace_root"])
        self.workspace_roots, self.owner_scope_root = [self.workspace_root], str(self.workspace_root)

    # LLM: 离线Bot不准备网络/MCP连接；已注册的本地工具保持可读，发现入口仍以冻结快照求交。
    # 函数用途: 防止一次Bot请求通过运行准备触发扩展联网。
    def prepare_for_run(self):
        return None

    # LLM: 原executor继续审批/落账；exact读取上界覆盖shared例外，现有pre_handler先行，只在handler前消耗额度。
    # 函数用途: 强制同一workspace目录墙和可写范围，再执行本请求的Bot预算门。
    def execute_tool(self, call, **kwargs):
        from ..tooling.models import ToolFailureStage, apply_tool_execution_facts

        parent_gate = kwargs.get("pre_handler_gate")

        # LLM: 该宿主闭包不进入模型参数；原拒绝优先，避免审批或原运行门拒绝时误扣预算。
        # 函数用途: 把Bot预算并入唯一原工具执行缝隙。
        def gate(proposed):
            denied = parent_gate(proposed) if parent_gate is not None else None
            if denied is not None:
                return denied
            denied = self._bot_guard.check(proposed)
            return (apply_tool_execution_facts(denied, failure_stage=ToolFailureStage.RUNTIME_GATE,
                handler_executed=False) if denied is not None else None)

        root = str(self.workspace_root)
        readonly = self._bot_guard.policy["bot_mode"] == "query_readonly"
        kwargs["write_boundary"] = {**(kwargs.get("write_boundary") or {}), "execution_cwd": root,
            "runtime_workspace_roots": [root], "effective_owner_scope_root": root,
            "read_scope_mode": "exact", "allowed_read_roots": [root],
            "allowed_write_roots": [] if readonly else [root]}
        kwargs["pre_handler_gate"] = gate
        return super().execute_tool(call, **kwargs)


# LLM: 使用原ModelScopedAttribute/ContextVar绑定，保留本次全部模型依赖；成功/异常均恢复，不替换共享Agent属性。
# 函数用途: 在worker实际调用期间绑定请求私有工具注册表，让并行工具继承同一预算与目录墙。
@contextmanager
def bot_execution_scope(agent, guard):
    if guard is None:
        yield
        return
    from ..settings.model_scope import (
        ModelScopedAttribute,
        ModelScopeDependencies,
        model_dependencies_scope,
    )

    if not isinstance(getattr(type(agent), "tools", None), ModelScopedAttribute):
        raise HostAPIError("BOT_TOOL_DENIED", 403)
    names = {name for cls in type(agent).__mro__ for name, value in vars(cls).items()
             if isinstance(value, ModelScopedAttribute)}
    values = {name: getattr(agent, name) for name in names if hasattr(agent, name)}
    values["tools"] = _BotToolRegistry(agent.tools, guard)
    with model_dependencies_scope(agent, ModelScopeDependencies(agent, tuple(values.items()))):
        yield
