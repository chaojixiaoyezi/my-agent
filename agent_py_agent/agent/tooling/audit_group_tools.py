# LLM: 两个工具仅使用宿主构造caller；请求不能携带installation_owner/as_user作为授权。
# 模块用途: 暴露审计组只读查询与黑盒调用的原生工具。
from __future__ import annotations

from pathlib import Path

from ..audit_group.access_policy import AuditGroupError, owner_identity, require_audit_group_member
from ..audit_group.blackbox_invoke_service import AuditGroupBlackboxInvokeService
from ..audit_group.observation_service import AuditGroupObservationService
from .input_schema import validate_tool_input
from .models import (
    ApprovalPolicy,
    BaseTool,
    EffectResolverPolicy,
    IdempotencyPolicy,
    ResourceScopePolicy,
    ToolAvailability,
    ToolModelHints,
    ToolModelSpec,
    ToolRuntimePolicy,
)
from .scripts_tool import _outcome

_QUERY_SCHEMA = {"type": "object", "required": ["action"], "additionalProperties": False, "properties": {
    "action": {"type": "string", "enum": ["owners", "assets", "alerts", "posture"],
        "description": "审计组只读查询动作：owners 列已登记用户，assets 汇总脱敏资产概况，alerts 查监控异常告警，posture 查看整体运行态势。"},
    "owner_ids": {"type": "array", "maxItems": 2000, "items": {"type": "string"},
        "description": "可选按准确 owner_id 列表过滤查询范围（如 [\"local/main\"]）。"},
    "target_owner": {"type": "string", "description": "可选指定单一目标 owner_id 做精确概况查询。"},
    "kinds": {"type": "array", "items": {"type": "string"},
        "description": "action=assets 时可选过滤资产类别列表（如 script、workflow、skill、monitor）。"},
    "cursor": {"type": "string", "maxLength": 256, "description": "分页游标，原样传入上一页返回的 next_cursor。"},
    "limit": {"type": "integer", "minimum": 1, "maximum": 200, "description": "单页返回最大条数（1..200）。"},
    "include_feishu": {"type": "boolean", "description": "action=owners 时是否包含飞书通道 owner。"},
    "include_local": {"type": "boolean", "description": "action=owners 时是否包含本地账号 owner。"},
    "severity": {"type": "string", "description": "action=alerts 时按告警级别过滤（如 error、warning）。"},
    "status": {"type": "string", "description": "action=alerts 时按告警状态过滤（如 open、resolved）。"},
    "since": {"type": "string", "description": "action=alerts 时按起始 ISO-8601 时间戳过滤该时刻之后的异常。"}}}
_INVOKE_SCHEMA = {"type": "object", "required": ["source_owner", "asset_type", "asset_id"],
    "additionalProperties": False, "properties": {
    "source_owner": {"type": "string", "maxLength": 530, "description": "被调用启用资产所属的来源 owner_id（如 local/main；不能填调用者自身）。"},
    "asset_type": {"type": "string", "enum": ["script", "skill", "workflow", "monitor", "monitor_rule"],
        "description": "黑盒调用的目标资产类型；复合规则监控可传 monitor 配合 rule_id，或传 monitor_rule。"},
    "asset_id": {"type": "string", "maxLength": 128, "description": "目标启用资产的准确安全 ID（monitor_rule 也支持 monitor_id:rule_id 简写）。"},
    "expected_revision": {"type": ["string", "integer", "null"], "description": "可选期望的来源资产版本号（CAS 校验，不匹配则拒绝执行）。"},
    "inputs": {"type": "object", "description": "传给目标黑盒资产的脱敏 JSON 输入参数对象。"},
    "rule_id": {"type": "string", "description": "调用多规则 monitor 时必选的目标规则编号。"},
    "timeout_s": {"type": "number", "exclusiveMinimum": 0, "maximum": 3600, "description": "本次隔离黑盒执行的超时秒数上限（0..3600）。"}}}


# LLM: 原生owner/home必须来自宿主且规范后缀一致；不借plugin_owner安装者或cwd猜身份。
# 函数用途: 为已有可信owner绑定注册两个工具，不给无身份注册表扩大跨用户权限。
def register_audit_group_tools(registry, params):
    if registry.owner_type not in {"main_agent", "user"}:
        return
    home = params.protected_persona_root or params.owner_scope_root
    caller = params.operation_owner_id
    if not home or not caller:
        return
    try:
        owner_identity(caller)
    except (AuditGroupError, ValueError):
        return
    suffix = ("owners", *caller.split("/"))
    path = Path(home).absolute()
    if path.parts[-len(suffix):] != suffix:
        return
    root = path.parents[len(suffix) - 1]
    registry.register(AuditGroupQueryTool(root, caller))
    registry.register(AuditGroupInvokeAssetTool(root, caller))


# LLM: 通用工具边界只使用固定caller，schema和实时权限均先于服务副作用；异常不回显路径或源正文。
# 类用途: 共用审计组工具的权限、可用性和安全错误投影。
class _AuditGroupTool(BaseTool):
    # LLM: 宿主固定真实owner，构造器不读写source或获取成员资格缓存。
    # 函数用途: 绑定每次调用需要重查的身份。
    def __init__(self, root_dir, caller_owner):
        owner_identity(caller_owner)
        self.root_dir, self.caller_owner = Path(root_dir).resolve(), caller_owner

    # LLM: availability只读当次账号；旧ToolRuntimeSnapshot仍由execute再次复核。
    # 函数用途: 决定本轮是否展示审计组工具。
    def availability(self):
        try:
            require_audit_group_member(self.root_dir, self.caller_owner)
            return ToolAvailability.ready()
        except AuditGroupError as error:
            return ToolAvailability.unavailable(error.message, error_code=error.code)

    # LLM: 审批前后复查也不能恢复已撤销资格。
    # 函数用途: 让原工具执行器即时看到撤权。
    def precheck_availability(self):
        return self.availability()

    # LLM: Schema不含caller/as_user/installation_owner；未知参数在任何业务执行前拒绝。
    # 函数用途: 验证工具封闭输入合同并保留具体字段校验问题。
    def validate_invocation(self, params, context):
        validation = validate_tool_input(params, self.model_spec.input_schema)
        if not validation.ok:
            issues = [issue.to_dict() for issue in validation.issues[:8]]
            truncated = len(validation.issues) > 8
            return _outcome(
                self.model_spec.name,
                {
                    "error_code": "TOOL_INVALID_ARGUMENTS",
                    "error": "参数不符合审计组工具封闭Schema",
                    "issues": issues,
                    "issues_truncated": truncated,
                    "effect_outcome": "not_started",
                    "error_details": {"issues": issues, "issues_truncated": truncated},
                },
                False,
                "TOOL_INVALID_ARGUMENTS",
                effect_outcome="not_started",
                next_action="按 error_details.issues[].path 修正字段类型与必填项，删除未声明的身份/路径字段后再试，不要原样重试。",
            )
        return None

    # LLM: 公共错误不含底层异常正文；只读查询失败无写入副作用，黑盒分派后失败可能有caller写入故保守标记unknown。
    # 函数用途: 执行身份固定的动作并转换为原生工具结果。
    def execute(self, params):
        invalid = self.validate_invocation(params, None)
        if invalid is not None:
            return invalid
        is_query = self.model_spec.name == "audit_group_query"
        dispatched = False
        try:
            require_audit_group_member(self.root_dir, self.caller_owner)
            dispatched = True
            return _outcome(self.model_spec.name, self.dispatch(params))
        except AuditGroupError as error:
            effect = "not_started" if (is_query or not dispatched) else "unknown"
            next_step = (
                "请核对审计组成员资格与查询过滤参数；此只读查询未产生任何写入副作用。"
                if is_query
                else (
                    "前置审计组资格核验未通过，尚未发起黑盒执行；请联系管理员核实当前账号审计组权限。"
                    if not dispatched
                    else "黑盒调用已进入执行阶段后中止（本地回执/产物副作用未知）：禁止盲目原样重试，请先核实错误码与目标资产状态。"
                )
            )
            return _outcome(
                self.model_spec.name,
                {
                    "error_code": error.code,
                    "message": error.message,
                    "status_code": error.status_code,
                    "effect_outcome": effect,
                    "error_details": {
                        "reason_code": error.code,
                        "status_code": error.status_code,
                        "dispatched": dispatched,
                    },
                },
                False,
                error.code,
                effect_outcome=effect,
                next_action=next_step,
            )
        except (ValueError, TypeError, KeyError, OSError) as exc:
            effect = "not_started" if (is_query or not dispatched) else "unknown"
            exc_type = type(exc).__name__
            next_step = (
                "审计组只读查询遇到存储或参数读取异常（未产生写入副作用）：请核对筛选参数后用 audit_group_query 重试。"
                if is_query
                else (
                    "黑盒调用在分派前异常终止（未启动执行）：请核实参数与审计组存储状态。"
                    if not dispatched
                    else "黑盒调用分派后发生异常（副作用保守未知）：切勿盲目自动重放，请先用 audit_group_query 核查状态或人工复核。"
                )
            )
            return _outcome(
                self.model_spec.name,
                {
                    "error_code": "AUDIT_GROUP_UNAVAILABLE",
                    "exception_type": exc_type,
                    "effect_outcome": effect,
                    "error_details": {
                        "reason_code": "AUDIT_GROUP_UNAVAILABLE",
                        "exception_type": exc_type,
                        "dispatched": dispatched,
                    },
                },
                False,
                "AUDIT_GROUP_UNAVAILABLE",
                effect_outcome=effect,
                next_action=next_step,
            )

# LLM: 冷绑定真实身份，每次execute必须实时复核。
# 类用途: 审计组跨owner查询工具。
class AuditGroupQueryTool(_AuditGroupTool):
    model_spec = ToolModelSpec("audit_group_query", "审计组只读查询已登记用户、资产白名单概况、监控异常和态势；不返回源码、密钥、Prompt或路径。",
        _QUERY_SCHEMA, hints=ToolModelHints(
            category="capability",
            use_cases=(
                "审计组成员跨 owner 只读查看已登记用户列表、脱敏资产摘要、监控失败告警与整体态势",
                "在发起 audit_group_invoke_asset 前先查询目标 owner 可调用资产 ID、类型与输入 Schema",
            ),
            avoid_when=(
                "查询当前用户自己的脚本/工作流/定时任务详情时直接使用 scripts / workflows / schedule",
                "需要实际执行他人启用资产计算结果时改用 audit_group_invoke_asset",
            ),
            examples=(
                '{"action":"owners","limit":20}',
                '{"action":"assets","target_owner":"local/main","kinds":["script","workflow"]}',
                '{"action":"alerts","severity":"error","status":"open"}',
            ),
            default_deferred=True,
            deferred_summary="审计组只读概况与异常查询"))
    runtime_policy = ToolRuntimePolicy(effect_resolver=EffectResolverPolicy("read_only"),
        resource_scopes=ResourceScopePolicy(mode="declared", static_scopes=("audit_group:observation",)))

    # LLM: action路由只选领域只读方法，非法动作组合不回落到其它工具。
    # 函数用途: 对选中的owner执行安全概况查询。
    def dispatch(self, params):
        service = AuditGroupObservationService(self.root_dir)
        method = {"owners": service.list_owners, "assets": service.summarize_assets,
                  "alerts": service.list_alerts, "posture": service.posture_overview}[params["action"]]
        return method(self.caller_owner, **{key: value for key, value in params.items() if key != "action"})


# LLM: 执行身份固定为caller，不切换至source。
# 类用途: 审计组受控资产调用工具。
class AuditGroupInvokeAssetTool(_AuditGroupTool):
    model_spec = ToolModelSpec("audit_group_invoke_asset", "以当前审计组调用者身份黑盒调用他人启用资产；结果归己，不复制源码，监控仅单次复核。",
        _INVOKE_SCHEMA, hints=ToolModelHints(
            category="capability",
            use_cases=(
                "审计组成员在隔离无网沙箱中黑盒调用其他 owner 已启用的 script/skill/workflow/monitor_rule",
                "对他人复合监控的指定规则做单次隔离复核并将脱敏产物归入当前调用者目录",
            ),
            avoid_when=(
                "仅查看他人有哪些资产或异常告警时先用只读 audit_group_query",
                "调用当前用户自己的脚本或工作流时用 scripts / workflows（不支持对自身 owner 调用本工具）",
            ),
            examples=(
                '{"source_owner":"local/main","asset_type":"script","asset_id":"daily_summary","inputs":{"limit":10}}',
            ),
            default_deferred=True,
            deferred_summary="四类资产只给用黑盒调用"))
    runtime_policy = ToolRuntimePolicy(effect_resolver=EffectResolverPolicy("dangerous"),
        approval_policy=ApprovalPolicy("dangerous"), idempotency_policy=IdempotencyPolicy("operation"),
        resource_scopes=ResourceScopePolicy(mode="declared", static_scopes=("audit_group:current_caller_execution",)))

    # LLM: 调用者永远来自宿主构造，不使用source_owner代执行或借插件安装者权限。
    # 函数用途: 将准确资产引用交给强制隔离调用器。
    def dispatch(self, params):
        return AuditGroupBlackboxInvokeService(self.root_dir).invoke_asset(self.caller_owner, **params)
