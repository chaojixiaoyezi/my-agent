# LLM: 管理员工具只供可信管理员主代理；只读查询不弹确认，授权变更沿 always_effects 保持管理员本人确认，auto/Full Access 不免除。
#   写入唯一走 owner_admin_controls.set_owner_admin_controls（目标 owner 自己的 tool_policy.json，锁内读改写）；
#   目标只接受 list 返回的规范 owner 编号，不创建用户目录，不从自然语言推断身份。
#   改动须同步 owner_admin_controls、audit_records 工具、错误码块与 test_decision_audit_controls。
# 模块用途: 让管理员管理用户决策/审计开关及准确外部资源授权，沿原用户工具策略保存，不分发宿主密钥。
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
# 拒绝原因码到错误码：身份/范围类是权限拒绝，策略文件坏了按持久化阻塞，其余按参数错误
_REASON_CODES = {"not_admin": "ADMIN_CONTROL_DENIED", "admin_self_only": "ADMIN_CONTROL_DENIED",
                 "policy_unreadable": "TOOL_PERSISTENCE_FAILED"}


# LLM: 回执只含 owner 编号、类型与三项布尔控制及来源，不含路径或策略文件其它字段；非管理员 owner 不显示跨用户审计项。
# 函数用途: 把一个 owner 的控制值整理成回执行。
def _owner_row(owner_id: str, home: object) -> dict:
    controls = read_owner_admin_controls(home)
    row = {"owner_id": owner_id, "owner_kind": getattr(home, "owner_kind", ""), **controls}
    if owner_id != "local/main":
        row.pop("cross_owner_audit_allowed")
    return row


# LLM: 与 core 注册条件配套：只有本机管理员主代理拿得到本工具；执行时仍按 home 复核管理员身份（替身调用也拒绝）。
# 类用途: 管理员直接查询控制与资源许可，所有授权修改仍需本人确认。
class AdminControlsTool(BaseTool):
    model_spec = ToolModelSpec(
        name=TOOL_NAME,
        description=(
            "管理员专用：查看或修改各用户的 Jev 决策模型使用权（decision_model_allowed）、审计工具使用权（audit_allowed），"
            "以及是否允许 my-agent 跨用户审计（cross_owner_audit_allowed，只能写在 owner_id=local/main 名下）。"
            "先 action=list 取得规范 owner_id，再 action=set 提交 owner_id 与 changes。"
            "resources 查看指定 owner 的云资源许可；resource_grant/resource_revoke 按 resource 精确登记/撤销。"
            "resource 含 provider、resource_type、resource_token、operations；飞书 sheet 操作为 sheet_read/sheet_append/sheet_update，"
            "docx 操作为 document_read/document_append，wiki 定位为 wiki_resolve；不填写密钥。"
            "list/resources 直接只读查询；set/resource_grant/resource_revoke 会请管理员本人确认，不要自行开启跨用户审计。"
        ),
        input_schema={
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["list", "set", "resources", "resource_grant", "resource_revoke"], "description": "list/set 用户控制；resources/resource_grant/resource_revoke 精确资源授权。"},
                "owner_id": {"type": "string", "description": "set 必填：list 返回的规范 owner_id，例如 local/main 或 providers/feishu/users/<id>。"},
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
            "管理员要求关闭或开启某个用户的 Jev 决策模型",
            "管理员要求关闭或开启某个用户的审计工具",
            "管理员明确允许或收回 my-agent 跨用户审计",
        ), avoid_when=("普通用户问自己的设置时（用 user_config 或 audit_records）",),
            default_deferred=True, deferred_summary="管理员专用：开关各用户的决策模型、审计权限和跨用户审计"),
    )
    runtime_policy = ToolRuntimePolicy(
        effect_resolver=EffectResolverPolicy("mutating", by_parameter=(("action", (("list", "read_only"), ("resources", "read_only"))),)),
        approval_policy=ApprovalPolicy("always", always_effects=("mutating", "dangerous")),
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

    # LLM: 用户控制只由主会话管理员操作，子代理运行里隐藏。
    # 函数用途: 子代理运行中报告本工具不可用。
    def availability(self) -> ToolAvailability:
        if current_subagent_run_id(self._agent):
            return ToolAvailability.unavailable("admin controls are managed only by the main administrator agent")
        return ToolAvailability.ready()

    # LLM: list/resources 只读；set 与资源授权复用原受保护策略和锁，子代理不能代表管理员变更用户授权。
    # 函数用途: 列出或修改用户控制与资源名单，返回不含凭据的结构化回执。
    def execute(self, params: dict) -> ToolHandlerOutcome:
        from ..user_space.approval_mode import is_permission_admin

        home = getattr(self._agent, "home_paths", None)
        action = params.get("action")
        if not is_permission_admin(home):
            return _refuse("not_admin", "只有本机管理员能使用此工具。")
        if current_subagent_run_id(self._agent):
            return _refuse("not_admin", "用户授权只能由管理员主会话管理。")
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
        target = owner_home_paths_by_id(home, params.get("owner_id"))
        if target is None:
            return _refuse("owner_not_found", "找不到这个 owner_id；请先 action=list 取得规范编号。")
        try:
            set_owner_admin_controls(home, target, params.get("changes"), actor=str(home.owner_id or "local/main"))
        except OwnerAdminControlsError as exc:
            return _refuse(exc.reason, str(exc))
        except OSError:
            return ToolHandlerOutcome(TOOL_NAME, False, "用户策略文件写入失败，请重新 list 核对是否已保存。",
                                      error_code="TOOL_PERSISTENCE_FAILED", effect_outcome="unknown")
        report = {"action": "set", "owner": _owner_row(params["owner_id"].strip(), target),
                  "effective": "下一次决策调用或审计调用即按新值生效，不需要重启。"}
        return ToolHandlerOutcome(TOOL_NAME, True, json.dumps(report, ensure_ascii=False))


# LLM: 目标沿原 owner_admin_controls resolver，不创建冷用户；资源写入仍是目标原 tool_policy 的单事务。
# 函数用途: 查看、登记或撤销准确云资源许可，不读或展示应用凭据。
def _resource_control(home, params):
    from ..user_space.resource_grants import read_resource_grants, set_resource_grant
    action = params['action']
    allowed = {'action', 'owner_id'} | ({'resource'} if action != 'resources' else set())
    if set(params) - allowed:
        return _refuse('invalid_changes', '资源管理只接受 owner_id 与对应 resource。')
    target = owner_home_paths_by_id(home, params.get('owner_id'))
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
    return ToolHandlerOutcome(TOOL_NAME, True, json.dumps({'action': action, 'owner_id': params['owner_id'], 'resources': rows}, ensure_ascii=False))


# LLM: 身份/范围类拒绝映射 ADMIN_CONTROL_DENIED，策略文件不可读映射 TOOL_PERSISTENCE_FAILED，参数与目标问题映射
#   TOOL_INVALID_ARGUMENTS；都在写入前发生（not_started），不留副作用。
# 函数用途: 生成统一的拒绝回执，带稳定原因码。
def _refuse(reason: str, message: str) -> ToolHandlerOutcome:
    code = _REASON_CODES.get(reason, "TOOL_INVALID_ARGUMENTS")
    return ToolHandlerOutcome(TOOL_NAME, False, json.dumps({"reason": reason, "message": message}, ensure_ascii=False),
                              error_code=code, effect_outcome="not_started", result_envelope={TOOL_NAME: {"reason": reason}})


__all__ = ["AdminControlsTool", "TOOL_NAME"]
