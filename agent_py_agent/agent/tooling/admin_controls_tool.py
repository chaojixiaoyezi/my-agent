# LLM: 管理员工具只供可信管理员主代理；已授权管理操作免逐次确认，执行时仍核 local/main 与主代理身份。
#   账号沿原 LocalUserLifecycle/Store；控制与资源仍写目标原 tool_policy.json，密码只进原账号散列字段。
#   本地用户名和规范 owner 引用只用于定位目标，不从模型参数或自然语言取得管理员身份。
#   改动须同步 owner_admin_controls、audit_records 工具、错误码块与 test_decision_audit_controls。
# 模块用途: 管理员代理直接管理普通账号、控制和精确资源许可，只返回公开用户状态。
from __future__ import annotations

import json

from ..runtime_context import current_subagent_run_id
from ..user_space.owner_admin_controls import (
    CONTROL_DEFAULTS,
    OwnerAdminControlsError,
    list_owner_home_paths,
    owner_home_paths_by_id,
    read_owner_admin_controls,
    set_owner_admin_controls,
)
from .models import (
    ApprovalPolicy,
    BaseTool,
    ConcurrencyPolicy,
    EffectResolverPolicy,
    IdempotencyPolicy,
    OutputPolicy,
    ResourceScopePolicy,
    SandboxPolicy,
    ToolAvailability,
    ToolHandlerOutcome,
    ToolModelHints,
    ToolModelSpec,
    ToolRuntimePolicy,
)

TOOL_NAME = "admin_controls"
# list 最多列出的用户数
_MAX_OWNERS_COUNT = 200
_ACTION_ALIASES = {"list_owners": "list", "set_controls": "set", "grant_resource": "resource_grant",
                   "revoke_resource": "resource_revoke"}
_USER_ACTIONS = {"create_user", "list_users", "get_user", "set_user_password", "enable_user", "disable_user"}
# 拒绝原因码到错误码：身份/范围类是权限拒绝，策略文件坏了按持久化阻塞，其余按参数错误
_REASON_CODES = {"not_admin": "ADMIN_CONTROL_DENIED", "admin_self_only": "ADMIN_CONTROL_DENIED",
                 "policy_unreadable": "TOOL_PERSISTENCE_FAILED"}


# LLM: 回执仅含 owner、控制及本地账号公开视图，不含路径、密码散列或其它策略；非管理员不展示跨用户审计项。
# 函数用途: 整理 owner 控制，本地已登记用户附加不含密码散列的公开状态。
def _owner_row(owner_id: str, home: object) -> dict:
    controls = read_owner_admin_controls(home)
    row = {"owner_id": owner_id, "owner_kind": getattr(home, "owner_kind", ""), **controls}
    if owner_id != "local/main":
        row.pop("cross_owner_audit_allowed")
    if owner_id.startswith("local/") and owner_id != "local/main":
        from ..gateway_parts.users_control_service import local_user_view
        from ..user_space.local_user_store import LocalUserStore
        store = LocalUserStore(home.root)
        uid = owner_id.split("/")[-1]
        if store.get(uid) is not None:
            row["local_user"] = local_user_view(store, uid)
    return row


# LLM: 与 core 注册条件配套：只有本机管理员主代理拿得到本工具；执行时仍按 home 复核管理员身份（替身调用也拒绝）。
# 类用途: 管理员主代理直接管理账号、控制与资源许可，所有操作复核可信身份。
class AdminControlsTool(BaseTool):
    model_spec = ToolModelSpec(
        name=TOOL_NAME,
        description=(
            "管理员专用：查看或修改各用户的 Jev 决策模型使用权（decision_model_allowed）、审计工具使用权（audit_allowed），"
            "以及是否允许 my-agent 跨用户审计（cross_owner_audit_allowed，只能写在 owner_id=local/main 名下）。"
            "list_owners/get 查询；set_controls 修改 controls，旧 list/set/changes 仍可用。"
            "create_user/list_users/get_user/set_user_password/enable_user/disable_user 管理本地账号。"
            "user_id 是准确用户名，owner_id 支持 local/alice、local/users/alice 或 alice；输出不含密码或散列。"
            "resources 查看指定 owner 的云资源许可；resource_grant/resource_revoke 按 resource 精确登记/撤销。"
            "resource 含 provider、resource_type、resource_token、operations；飞书 sheet 操作为 sheet_read/sheet_append/sheet_update，"
            "docx 操作为 document_read/document_append，wiki 定位为 wiki_resolve；不填写密钥。"
            "list/resources 只读查询；set/resource_grant/resource_revoke 执行管理员已要求的修改，不要自行开启跨用户审计。"
        ),
        input_schema={
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["get", "list_owners", "set_controls", "grant_resource", "revoke_resource",
                    "create_user", "list_users", "get_user", "set_user_password", "enable_user", "disable_user",
                    "list", "set", "resources", "resource_grant", "resource_revoke"], "description": "管理账号、控制和准确资源许可，兼容旧动作。"},
                "owner_id": {"type": "string", "description": "set 必填：list 返回的规范 owner_id，例如 local/main 或 providers/feishu/users/<id>。"},
                "user_id": {"type": "string", "description": "本地普通用户名，可由 owner_id 推导。"},
                "password": {"type": "string", "description": "初始或新密码，至少8位且含大小写字母与数字，不回显。"},
                "display_name": {"type": "string", "description": "创建时的可选显示名称。"},
                "quotas": {"type": "object", "description": "创建时的原用户配额。"},
                "reason": {"type": "string", "description": "停用原因。"},
                "controls": {"type": "object", "properties": {key: {"type": "boolean"} for key in CONTROL_DEFAULTS},
                    "additionalProperties": False, "minProperties": 1},
                "changes": {
                    "type": "object",
                    "properties": {key: {"type": "boolean"} for key in CONTROL_DEFAULTS},
                    "additionalProperties": False, "minProperties": 1,
                    "description": "set 必填：要修改的开关，值为 true/false；cross_owner_audit_allowed 只能对 local/main 设置。",
                },
                "limit": {"type": "integer", "minimum": 1, "maximum": _MAX_OWNERS_COUNT, "description": "list 最多列出的用户数，默认 50。"},
                "resource": {"type": "object", "properties": {
                    "provider": {"type": "string"}, "resource_type": {"type": "string"},
                    "resource_token": {"type": "string"}, "operations": {"type": "array", "items": {"type": "string"}},
                }, "required": ["provider", "resource_type", "resource_token", "operations"], "additionalProperties": False},
            },
            "required": ["action"],
            "additionalProperties": False,
        },
        hints=ToolModelHints(category="system", use_cases=(
            "管理员要求创建本地用户或重置登录密码、停用和重新启用账号",
            "管理员要求关闭或开启某个用户的 Jev 决策模型",
            "管理员要求关闭或开启某个用户的审计工具",
            "管理员明确允许或收回 my-agent 跨用户审计",
        ), avoid_when=("普通用户问自己的设置时（用 user_config 或 audit_records）",),
            default_deferred=True, deferred_summary="管理员专用：创建和管理本地用户、决策/审计控制及精确资源授权"),
    )
    runtime_policy = ToolRuntimePolicy(
        effect_resolver=EffectResolverPolicy("mutating", by_parameter=(("action", tuple((action, "read_only") for action in
            ("list", "resources", "get", "list_owners", "list_users", "get_user"))),)),
        approval_policy=ApprovalPolicy("never"),
        sandbox_policy=SandboxPolicy("none"),
        concurrency_policy=ConcurrencyPolicy("serial"),
        idempotency_policy=IdempotencyPolicy("operation"),
        resource_scopes=ResourceScopePolicy(mode="declared", static_scopes=("admin_controls:owners",)),
        output_policy=OutputPolicy(trust="runtime"),
        promotes_task=False,
        mutates_workspace=False,
    )

    # LLM: 这里只绑定宿主实例，不能从模型 owner_id 替换当前管理员身份。
    # 函数用途: 保存宿主 agent，身份在执行时从它的 home_paths 读取。
    def __init__(self, agent: object) -> None:
        self._agent = agent

    # LLM: 注册之外再核可信 local/main 与当前 runner/registry 主代理身份，不认显示名或模型参数。
    # 函数用途: 非管理员或子代理查询本工具时明确报告不可用。
    def availability(self) -> ToolAvailability:
        from ..user_space.approval_mode import is_permission_admin

        if (not is_permission_admin(getattr(self._agent, "home_paths", None))
                or current_subagent_run_id(self._agent)
                or getattr(getattr(self._agent, "tools", None), "owner_type", "main_agent") != "main_agent"):
            return ToolAvailability.unavailable("admin controls are managed only by the main administrator agent")
        return ToolAvailability.ready()

    # LLM: list/resources 只读；写入复用原受保护策略与锁，执行复核 local/main 和 runner/registry，不能借管理员对象冒充主代理。
    # 函数用途: 在主代理权限门后执行原控制与本地账号管理，返回不含凭据的回执。
    def execute(self, params: dict) -> ToolHandlerOutcome:
        from ..user_space.approval_mode import is_permission_admin

        home = getattr(self._agent, "home_paths", None)
        action = params.get("action")
        if not is_permission_admin(home):
            return _refuse("not_admin", "只有本机管理员能使用此工具。")
        if (current_subagent_run_id(self._agent)
                or getattr(getattr(self._agent, "tools", None), "owner_type", "main_agent") != "main_agent"):
            return _refuse("not_admin", "用户授权只能由管理员主会话管理。")
        if action in _USER_ACTIONS:
            return _user_control(self._agent, params)
        params = dict(params, action=_ACTION_ALIASES.get(action, action))
        action = params["action"]
        if "controls" in params:
            if "changes" in params:
                return _refuse("invalid_changes", "controls 与 changes 不可同时填写。")
            params["changes"] = params.pop("controls")
        if action == "get":
            target = _target_home(home, params.get("owner_id"))
            if target is None or set(params) - {"action", "owner_id"}:
                return _refuse("owner_not_found", "目标不存在或参数不合法。")
            return ToolHandlerOutcome(TOOL_NAME, True, json.dumps({"action": "get", "owner": _owner_row(target.owner_id, target)}, ensure_ascii=False))
        if action in {"resources", "resource_grant", "resource_revoke"}:
            return _resource_control(home, params)
        if action == "list":
            limit = params.get("limit", 50)
            if type(limit) is not int or not 1 <= limit <= _MAX_OWNERS_COUNT:
                return _refuse("invalid_changes", f"limit 须为 1 到 {_MAX_OWNERS_COUNT} 的整数。")
            owners, truncated = list_owner_home_paths(home, limit=limit)
            report = {"action": "list", "owners": [_owner_row(owner_id, owner_home) for owner_id, owner_home in owners],
                      "truncated": truncated}
            return ToolHandlerOutcome(TOOL_NAME, True, json.dumps(report, ensure_ascii=False))
        if action != "set" or set(params) - {"action", "owner_id", "changes"}:
            return _refuse("invalid_changes", "action 须为 list 或 set；set 只接受 owner_id 与 changes。")
        target = _target_home(home, params.get("owner_id"))
        if target is None:
            return _refuse("owner_not_found", "找不到这个 owner_id；请先 action=list 取得规范编号。")
        try:
            set_owner_admin_controls(home, target, params.get("changes"), actor=str(home.owner_id or "local/main"))
        except OwnerAdminControlsError as exc:
            return _refuse(exc.reason, str(exc))
        except OSError:
            return ToolHandlerOutcome(TOOL_NAME, False, "用户策略文件写入失败，请重新 list 核对是否已保存。",
                                      error_code="TOOL_PERSISTENCE_FAILED", effect_outcome="unknown")
        report = {"action": "set", "owner": _owner_row(target.owner_id, target),
                  "effective": "下一次决策调用或审计调用即按新值生效，不需要重启。"}
        return ToolHandlerOutcome(TOOL_NAME, True, json.dumps(report, ensure_ascii=False))


# LLM: 目标沿原 owner_admin_controls resolver，不创建冷用户；资源写入仍是目标原 tool_policy 的单事务。
# 函数用途: 以规范 owner 或普通本地用户名查看和修改原云资源许可，不读凭据。
def _resource_control(home, params):
    from ..user_space.resource_grants import read_resource_grants, set_resource_grant
    action = params['action']
    allowed = {'action', 'owner_id'} | ({'resource'} if action != 'resources' else set())
    if set(params) - allowed:
        return _refuse('invalid_changes', '资源管理只接受 owner_id 与对应 resource。')
    target = _target_home(home, params.get('owner_id'))
    if target is None:
        return _refuse('owner_not_found', '找不到这个 owner_id；请先 action=list 取得规范编号。')
    try:
        rows = read_resource_grants(target) if action == 'resources' else set_resource_grant(
            home, target, params.get('resource'), revoke=action == 'resource_revoke')
    except (ValueError, PermissionError) as exc:
        return _refuse('invalid_changes', str(exc))
    except OSError:
        return ToolHandlerOutcome(TOOL_NAME, False, '资源授权保存失败，请重新 resources 核对。',
            error_code='TOOL_PERSISTENCE_FAILED', effect_outcome='unknown' if action != 'resources' else 'not_started')
    return ToolHandlerOutcome(TOOL_NAME, True, json.dumps({'action': action, 'owner_id': target.owner_id, 'resources': rows}, ensure_ascii=False))


# LLM: 裸用户名仅按严格本地 UID 规范化；路径和 provider 引用仍由原 resolver 验证，不创建冷目录。
# 函数用途: 为管理员控制和资源动作定位同一个普通用户空间。
def _target_home(home, owner_id):
    if type(owner_id) is str and "/" not in owner_id:
        owner_id = f"local/{owner_id}"
    return owner_home_paths_by_id(home, owner_id)


# LLM: 权限在 execute 先核；Gateway 根仅从可信配置读；部分创建沿原错误分类保留原报码和恢复详情，不标为未开始。
# 函数用途: 将账号动作及真实宿主队列交给共享服务，错误不回显秘密。
def _user_control(agent, params):
    from ..gateway_parts.users_control_service import (
        LocalUserInitializationIncomplete,
        manage_local_users,
    )
    from ..plugin_host.errors import HostAPIError
    home = agent.home_paths
    root = None
    if getattr(agent, "root", None) is not None and getattr(getattr(agent, "config", None), "gateway_workspace", None):
        from ..gateway_parts.paths import gateway_paths
        root = gateway_paths(agent).root
    allowed = {"action", "owner_id", "user_id"}
    extra = {"create_user": {"password", "display_name", "quotas", "controls"}, "set_user_password": {"password"}, "disable_user": {"reason"}}
    if set(params) - (allowed | extra.get(params["action"], set())):
        return _refuse("invalid_changes", "用户管理参数不合法。")
    try:
        report = manage_local_users(home, params["action"], params, gateway_root=root)
    except LocalUserInitializationIncomplete as exc:
        report = {"ok": False, "action": params["action"], "error_code": exc.code,
                  "message": exc.message, "details": exc.public_details}
        return ToolHandlerOutcome(TOOL_NAME, False, json.dumps(report, ensure_ascii=False),
            error_code="TOOL_PERSISTENCE_FAILED", reported_error_code=exc.code, effect_outcome="unknown")
    except OwnerAdminControlsError as exc:
        return _refuse(exc.reason, "用户控制无效，未完成操作。")
    except HostAPIError as exc:
        return ToolHandlerOutcome(TOOL_NAME, False, "用户管理未完成，请查询当前状态。", error_code=exc.code, effect_outcome="unknown")
    except ValueError:
        return _refuse("invalid_changes", "用户名、密码、配额或其它参数无效。")
    except OSError:
        return ToolHandlerOutcome(TOOL_NAME, False, "用户管理保存失败，请查询当前状态。", error_code="TOOL_PERSISTENCE_FAILED", effect_outcome="unknown")
    return ToolHandlerOutcome(TOOL_NAME, True, json.dumps(report, ensure_ascii=False))


# LLM: 身份/范围类拒绝映射 ADMIN_CONTROL_DENIED，策略文件不可读映射 TOOL_PERSISTENCE_FAILED，参数与目标问题映射
#   TOOL_INVALID_ARGUMENTS；都在写入前发生（not_started），不留副作用。
# 函数用途: 生成统一的拒绝回执，带稳定原因码。
def _refuse(reason: str, message: str) -> ToolHandlerOutcome:
    code = _REASON_CODES.get(reason, "TOOL_INVALID_ARGUMENTS")
    return ToolHandlerOutcome(TOOL_NAME, False, json.dumps({"reason": reason, "message": message}, ensure_ascii=False),
                              error_code=code, effect_outcome="not_started", result_envelope={TOOL_NAME: {"reason": reason}})


__all__ = ["AdminControlsTool", "TOOL_NAME"]
