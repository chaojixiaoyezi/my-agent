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
    "action": {"type": "string", "enum": ["owners", "assets", "alerts", "posture"]},
    "owner_ids": {"type": "array", "maxItems": 2000, "items": {"type": "string"}},
    "target_owner": {"type": "string"}, "kinds": {"type": "array", "items": {"type": "string"}},
    "cursor": {"type": "string", "maxLength": 256}, "limit": {"type": "integer", "minimum": 1, "maximum": 200},
    "include_feishu": {"type": "boolean"}, "include_local": {"type": "boolean"},
    "severity": {"type": "string"}, "status": {"type": "string"}, "since": {"type": "string"}}}
_INVOKE_SCHEMA = {"type": "object", "required": ["source_owner", "asset_type", "asset_id"],
    "additionalProperties": False, "properties": {"source_owner": {"type": "string", "maxLength": 530},
    "asset_type": {"type": "string", "enum": ["script", "skill", "workflow", "monitor"]},
    "asset_id": {"type": "string", "maxLength": 128}, "expected_revision": {"type": ["string", "integer", "null"]},
    "inputs": {"type": "object"}, "rule_id": {"type": "string"},
    "timeout_s": {"type": "number", "exclusiveMinimum": 0, "maximum": 3600}}}


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
    # 函数用途: 验证工具封闭输入合同。
    def validate_invocation(self, params, context):
        if not validate_tool_input(params, self.model_spec.input_schema).ok:
            return _outcome(self.model_spec.name, {"error_code": "TOOL_INVALID_ARGUMENTS"}, False, "TOOL_INVALID_ARGUMENTS")
        return None

    # LLM: 公共错误不含底层异常正文；黑盒失败可能有caller写入，保守标记unknown，不能自动重放。
    # 函数用途: 执行身份固定的动作并转换为原生工具结果。
    def execute(self, params):
        invalid = self.validate_invocation(params, None)
        if invalid is not None:
            return invalid
        try:
            require_audit_group_member(self.root_dir, self.caller_owner)
            return _outcome(self.model_spec.name, self.dispatch(params))
        except AuditGroupError as error:
            return _outcome(self.model_spec.name, {"error_code": error.code, "message": error.message,
                "status_code": error.status_code}, False, error.code,
                effect_outcome="unknown" if self.model_spec.name == "audit_group_invoke_asset" else "failed")
        except (ValueError, TypeError, KeyError, OSError):
            return _outcome(self.model_spec.name, {"error_code": "AUDIT_GROUP_UNAVAILABLE"}, False,
                            "AUDIT_GROUP_UNAVAILABLE", effect_outcome="unknown")

# LLM: 冷绑定真实身份，每次execute必须实时复核。
# 类用途: 审计组跨owner查询工具。
class AuditGroupQueryTool(_AuditGroupTool):
    model_spec = ToolModelSpec("audit_group_query", "审计组只读查询已登记用户、资产白名单概况、监控异常和态势；不返回源码、密钥、Prompt或路径。",
        _QUERY_SCHEMA, hints=ToolModelHints(category="capability", default_deferred=True, deferred_summary="审计组只读概况与异常查询"))
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
        _INVOKE_SCHEMA, hints=ToolModelHints(category="capability", default_deferred=True, deferred_summary="四类资产只给用黑盒调用"))
    runtime_policy = ToolRuntimePolicy(effect_resolver=EffectResolverPolicy("dangerous"),
        approval_policy=ApprovalPolicy("dangerous"), idempotency_policy=IdempotencyPolicy("operation"),
        resource_scopes=ResourceScopePolicy(mode="declared", static_scopes=("audit_group:current_caller_execution",)))

    # LLM: 调用者永远来自宿主构造，不使用source_owner代执行或借插件安装者权限。
    # 函数用途: 将准确资产引用交给强制隔离调用器。
    def dispatch(self, params):
        return AuditGroupBlackboxInvokeService(self.root_dir).invoke_asset(self.caller_owner, **params)
