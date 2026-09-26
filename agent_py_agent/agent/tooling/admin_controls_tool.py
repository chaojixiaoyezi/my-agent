# LLM: 管理员专用管控工具：只在 is_permission_admin(当前 home) 时由 core 注册，子代理不可用；每次调用都要管理员本人确认
#   （ApprovalPolicy always，自主/Full Access 模式也不免确认），保证"让 my-agent 跨用户审计"只能由管理员明确允许。
#   写入唯一走 owner_admin_controls.set_owner_admin_controls（目标 owner 自己的 tool_policy.json，锁内读改写）；
#   目标只接受 list 返回的规范 owner 编号，不创建用户目录，不从自然语言推断身份。
#   改动须同步 owner_admin_controls、audit_records 工具、错误码块与 test_decision_audit_controls。
# 模块用途: 让管理员开关某个用户的 Jev 决策使用权、审计工具使用权，以及 my-agent 能否跨用户审计。
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
_MAX_OWNERS = 200
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
# 类用途: 管理员查看并修改各用户的 Jev / 审计开关和自己的跨用户审计许可。
class AdminControlsTool(BaseTool):
    model_spec = ToolModelSpec(
        name=TOOL_NAME,
        description=(
            "管理员专用：查看或修改各用户的 Jev 决策模型使用权（decision_model_allowed）、审计工具使用权（audit_allowed），"
            "以及是否允许 my-agent 跨用户审计（cross_owner_audit_allowed，只能写在 owner_id=local/main 名下）。"
            "先 action=list 取得规范 owner_id，再 action=set 提交 owner_id 与 changes。"
            "每次调用都会请管理员本人确认；只在管理员明确要求时使用，不要自行开启跨用户审计。"
        ),
        input_schema={
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["list", "set"], "description": "list 查看全部用户的开关；set 修改一个用户。"},
                "owner_id": {"type": "string", "description": "set 必填：list 返回的规范 owner_id，例如 local/main 或 providers/feishu/users/<id>。"},
                "changes": {
                    "type": "object",
                    "properties": {key: {"type": "boolean"} for key in CONTROL_DEFAULTS},
                    "additionalProperties": False, "minProperties": 1,
                    "description": "set 必填：要修改的开关，值为 true/false；cross_owner_audit_allowed 只能对 local/main 设置。",
                },
                "limit": {"type": "integer", "minimum": 1, "maximum": _MAX_OWNERS, "description": "list 最多列出的用户数，默认 50。"},
            },
            "required": ["action"],
            "additionalProperties": False,
        },
        hints=ToolModelHints(category="system", use_cases=(
            "管理员要求关闭或开启某个用户的 Jev 决策模型",
            "管理员要求关闭或开启某个用户的审计工具",
            "管理员明确允许或收回 my-agent 跨用户审计",
        ), avoid_when=("普通用户问自己的设置时（用 user_config 或 audit_records）",)),
    )
    runtime_policy = ToolRuntimePolicy(
        effect_resolver=EffectResolverPolicy("mutating", by_parameter=(("action", (("list", "read_only"),)),)),
        approval_policy=ApprovalPolicy("always"),
        sandbox_policy=SandboxPolicy("none"),
        concurrency_policy=ConcurrencyPolicy("serial"),
        idempotency_policy=IdempotencyPolicy("operation"),
        resource_scopes=ResourceScopePolicy(mode="declared", static_scopes=("admin_controls:owners",)),
        output_policy=OutputPolicy(trust="runtime"),
        promotes_task=False,
        mutates_workspace=False,
    )

    # 函数用途: 保存宿主 agent，身份在执行时从它的 home_paths 读取。
    def __init__(self, agent: object) -> None:
        self._agent = agent

    # LLM: 用户控制只由主会话管理员操作，子代理运行里隐藏。
    # 函数用途: 子代理运行中报告本工具不可用。
    def availability(self) -> ToolAvailability:
        if current_subagent_run_id(self._agent):
            return ToolAvailability.unavailable("admin controls are managed only by the main administrator agent")
        return ToolAvailability.ready()

    # LLM: list 只读；set 只经 set_owner_admin_controls 写目标 owner 策略文件（副作用），拒绝一律 not_started。
    # 函数用途: 按 action 列出或修改用户控制，返回结构化回执。
    def execute(self, params: dict) -> ToolHandlerOutcome:
        from ..user_space.approval_mode import is_permission_admin

        home = getattr(self._agent, "home_paths", None)
        action = params.get("action")
        if not is_permission_admin(home):
            return _refuse("not_admin", "只有本机管理员能使用此工具。")
        if action == "list":
            limit = params.get("limit", 50)
            if type(limit) is not int or not 1 <= limit <= _MAX_OWNERS:
                return _refuse("invalid_changes", f"limit 须为 1 到 {_MAX_OWNERS} 的整数。")
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


# LLM: 身份/范围类拒绝映射 ADMIN_CONTROL_DENIED，策略文件不可读映射 TOOL_PERSISTENCE_FAILED，参数与目标问题映射
#   TOOL_INVALID_ARGUMENTS；都在写入前发生（not_started），不留副作用。
# 函数用途: 生成统一的拒绝回执，带稳定原因码。
def _refuse(reason: str, message: str) -> ToolHandlerOutcome:
    code = _REASON_CODES.get(reason, "TOOL_INVALID_ARGUMENTS")
    return ToolHandlerOutcome(TOOL_NAME, False, json.dumps({"reason": reason, "message": message}, ensure_ascii=False),
                              error_code=code, effect_outcome="not_started", result_envelope={TOOL_NAME: {"reason": reason}})


__all__ = ["AdminControlsTool", "TOOL_NAME"]
