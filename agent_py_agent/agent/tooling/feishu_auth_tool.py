# LLM: 本人OAuth是明确输入交互，不向模型返回认证材料；工具注册与目录沿原渐进发现。
# 模块用途: 让私人助手查询、发起或撤销本人飞书授权。
from __future__ import annotations

import json

from ..user_space.feishu_oauth import (
    _application_credentials,
    _assert_network,
    oauth_configuration,
    oauth_status,
    request_oauth,
)
from ..user_space.owner_resolver import owner_identity_from_config
from .models import (
    BaseTool,
    EffectResolverPolicy,
    IdempotencyPolicy,
    ToolAvailability,
    ToolHandlerOutcome,
    ToolInputPolicy,
    ToolModelHints,
    ToolModelSpec,
    ToolParameterCondition,
    ToolRuntimePolicy,
    TrustedParameterBinding,
)


# LLM: schema仅有固定交互动作；authorize 沿原条件晋升持久任务，status/revoke 不凭查询创建任务；身份与范围取宿主。
# 类用途: 为本人飞书资源建立独立授权。
class FeishuAuthTool(BaseTool):
    model_spec = ToolModelSpec(name="feishu_auth", description=(
        "查询、发起或撤销当前用户的本人飞书OAuth授权。status查询，authorize向原发起人私聊发送登录授权卡，"
        "revoke撤销宿主保存的本人授权。无需用户发送应用密钥或token；群共享资源用feishu_cloud应用身份。"
        "authorize会将任务保存为待输入并释放执行资源，填写后恢复原任务。"),
        input_schema={"type": "object", "properties": {"action": {"type": "string", "enum": ["status", "authorize", "revoke"]}},
                      "required": ["action"], "additionalProperties": False},
        hints=ToolModelHints(category="network", keywords=("飞书", "本人", "登录", "授权", "OAuth"),
                             default_deferred=True, deferred_summary="查询或建立本人飞书授权，登录卡私发，宿主保存凭据"))
    runtime_policy = ToolRuntimePolicy(effect_resolver=EffectResolverPolicy("mutating",
        by_parameter=(("action", (("status", "read_only"),)),)), idempotency_policy=IdempotencyPolicy("operation"),
        promotes_task_when=(ToolParameterCondition('action', 'equals', 'authorize'),),
        input_policy=ToolInputPolicy(internal_parameters=("__network_access_mode",), trusted_parameter_bindings=(
            ("__network_access_mode", TrustedParameterBinding(source_refs=("write_boundary.network_access_mode",), authority="host_authoritative")),)))

    # LLM: 注册不读token、不联网、不创建授权。
    # 函数用途: 保存可信当前Agent。
    def __init__(self, agent):
        self.agent = agent

    # LLM: 功能与部署状态如实反映；群不拥有共享个人OAuth身份。
    # 函数用途: 未配置回调或群身份时给明确不可用原因。
    def availability(self):
        try:
            if owner_identity_from_config(self.agent.config).owner_kind == "group":
                return ToolAvailability.unavailable("群资源使用应用身份；本人授权在私人助手会话进行。")
            oauth_configuration(self.agent)
            _application_credentials(self.agent)
        except (OSError, ValueError, TypeError):
            return ToolAvailability.unavailable("本人飞书授权尚未配置，请部署者登记回调和应用用户权限。")
        return ToolAvailability.ready()

    # LLM: 返回仅有状态/引用，无state链接或密码；等待依赖让原模型循环在完整归档后结束。
    # 函数用途: 查询或执行一次本人授权操作。
    def execute(self, params):
        if params.get("action") not in {"status", "authorize", "revoke"} or set(params) - {"action", "__network_access_mode"}:
            return ToolHandlerOutcome("feishu_auth", False, "本人授权动作无效。", error_code="TOOL_INVALID_ARGUMENTS")
        if not self.availability().available:
            return ToolHandlerOutcome("feishu_auth", False, self.availability().reason, error_code="TOOL_UNAVAILABLE")
        try:
            if params["action"] == "authorize":
                try:
                    _assert_network(self.agent, invocation_mode=params.get("__network_access_mode"))
                except (OSError, ValueError, TypeError, KeyError):
                    return ToolHandlerOutcome("feishu_auth", False, "当前任务网络权限不可用，未发起本人授权。",
                        error_code="FEISHU_USER_AUTH_REQUIRED", effect_outcome="not_started")
                dependency = request_oauth(self.agent, invocation_mode=params.get("__network_access_mode"))
                return ToolHandlerOutcome("feishu_auth", True, json.dumps(dependency, ensure_ascii=False),
                                           result_envelope={"input_dependency": dependency})
            return ToolHandlerOutcome("feishu_auth", True, json.dumps(
                oauth_status(self.agent, revoke=params["action"] == "revoke"), ensure_ascii=False))
        except (OSError, ValueError, TypeError, KeyError):
            return ToolHandlerOutcome("feishu_auth", False, "本人授权不可用，请检查部署配置、原私人身份和任务状态。",
                                      error_code="FEISHU_USER_AUTH_REQUIRED")
