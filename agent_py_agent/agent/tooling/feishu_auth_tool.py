# LLM: 本人OAuth是明确输入交互，不向模型返回认证材料；工具注册与目录沿原渐进发现。
# 模块用途: 让私人助手查询、发起或撤销本人飞书授权。
from __future__ import annotations

import json

from ..user_space.feishu_authorization import authorization_guidance
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


def _available_auth_entry_advice(agent, guidance: dict, *, group_owner: bool = False) -> tuple[str, str]:
    if group_owner:
        return (
            'feishu_auth(action="permissions")',
            '群会话无共享个人 OAuth 身份（未发起请求）：群共享文档/消息请改用应用身份工具（feishu_cloud identity="bot" 或 channel_messages selector="current_group"）；'
            '查看应用权限清单可调 feishu_auth(action="permissions")，本人 OAuth 登录请在用户私人助手会话中执行 feishu_auth(action="authorize")。',
        )
    if guidance.get("configuration_status") != "configured":
        url = guidance.get("permission_settings_url") or ""
        url_hint = f"（开发者后台权限页：{url}）" if url else ""
        return (
            'feishu_auth(action="permissions")',
            f'宿主尚未完成本人 OAuth 应用凭据或 HTTPS 回调配置（未发起请求）：当前仅可调用只读 feishu_auth(action="permissions") 查看所需权限与配置说明{url_hint}，'
            '请先由部署者配置 feishu_app_id/feishu_app_secret 与 /feishu/oauth/callback 后再发起 authorize。',
        )
    if guidance.get("oauth_status") == "current_personal_identity_required":
        return (
            'feishu_auth(action="permissions")',
            '当前会话缺少可用的本人飞书通道身份绑定（未发起请求）：可调用只读 feishu_auth(action="permissions") 查看应用权限要求，'
            '并在本人飞书私聊助手会话或恢复管理员飞书通道绑定后调用 feishu_auth(action="authorize")。',
        )
    return (
        'feishu_auth(action="status")',
        '请先调用只读 feishu_auth(action="status") 核对当前授权状态与 required_action，再按指引执行下一步。',
    )


def _classify_oauth_runtime_failure(agent, action: str, exc: Exception, guidance: dict) -> tuple[str, str, str, dict]:
    text = str(exc) if isinstance(exc, ValueError) else ""
    if "已有待处理请求" in text:
        return (
            "本人飞书授权已有未到期的待处理登录卡，未重复发送新卡。",
            "not_started",
            '请提示用户完成已私发的飞书登录授权卡，或先调用 feishu_auth(action="status") 核实、必要时调用 feishu_auth(action="revoke") 撤销旧请求后再重新 authorize。',
            {"reason_code": "OAUTH_PENDING_REQUEST_EXISTS", "action": action, "available_entry": 'feishu_auth(action="status")'},
        )
    if "网络权限不可用" in text:
        return (
            "当前任务或子代理网络权限已禁用，未发起本人飞书授权。",
            "not_started",
            '可先调用只读 feishu_auth(action="permissions") 查看所需权限清单，并在开启网络权限的主会话中执行 feishu_auth(action="authorize")。',
            {"reason_code": "NETWORK_DISABLED", "action": action, "available_entry": 'feishu_auth(action="permissions")'},
        )
    if any(token in text for token in ("群资源使用应用身份", "没有本人飞书身份", "管理员本人绑定已失效")):
        entry, advice = _available_auth_entry_advice(agent, guidance, group_owner=("群资源使用应用身份" in text))
        return (
            text,
            "not_started",
            advice,
            {"reason_code": "PERSONAL_IDENTITY_UNAVAILABLE", "action": action, "available_entry": entry},
        )
    if any(token in text for token in ("没有有效执行上下文", "任务执行已失效")):
        return (
            text,
            "not_started",
            '本次授权前置执行范围核验未通过（未发起请求）：可先调用只读 feishu_auth(action="status") 查询当前授权状态，并在活跃用户会话回合中重新发起 authorize。',
            {"reason_code": "EXECUTION_SCOPE_INACTIVE", "action": action, "available_entry": 'feishu_auth(action="status")'},
        )
    if any(token in text for token in ("授权功能未启用", "回调", "范围配置无效", "飞书应用尚未配置")):
        entry, advice = _available_auth_entry_advice(agent, guidance)
        return (
            text,
            "not_started",
            advice,
            {"reason_code": "OAUTH_CONFIGURATION_UNAVAILABLE", "action": action, "available_entry": entry},
        )
    if "未送达或输入登记失败" in text:
        return (
            "本人飞书授权卡发送或待输入登记失败，本次挂起请求已自动收口为失败状态。",
            "failed",
            '请先调用 feishu_auth(action="status") 确认当前记录已收口且飞书私聊通道可达，再重新调用 feishu_auth(action="authorize")。',
            {"reason_code": "OAUTH_CARD_DELIVERY_FAILED", "action": action, "available_entry": 'feishu_auth(action="status")'},
        )
    exc_type = type(exc).__name__
    effect = "not_started" if action in {"permissions", "status"} else "unknown"
    return (
        f"本人飞书授权操作遇到存储或运行时异常（{exc_type}）。",
        effect,
        '请先调用只读 feishu_auth(action="status") 或 feishu_auth(action="permissions") 核实当前授权状态，切勿盲目连续重复提交。',
        {"reason_code": "OAUTH_RUNTIME_ERROR", "action": action, "exception_type": exc_type, "available_entry": 'feishu_auth(action="status")'},
    )


# LLM: schema仅有固定交互动作；authorize 沿原条件晋升持久任务，status/revoke 不凭查询创建任务；身份与范围取宿主。
# 类用途: 为本人飞书资源建立独立授权。
class FeishuAuthTool(BaseTool):
    model_spec = ToolModelSpec(name="feishu_auth", description=(
        "查询当前飞书应用所需权限和直达开发者后台的申请链接，或发起/撤销本人OAuth。"
        "permissions查询私聊、群历史等能力的权限清单、后台链接和配置阻塞；status同时报告本人授权状态。"
        "用户问如何开权限时先调用本工具，不猜应用ID或声称已经授权。authorize向原发起人私聊发送登录授权卡，"
        "revoke撤销宿主保存的本人授权。无需用户发送应用密钥或token；群共享资源用feishu_cloud应用身份。"
        "authorize会将任务保存为待输入并释放执行资源，填写后恢复原任务。"),
        input_schema={"type": "object", "properties": {
            "action": {"type": "string", "enum": ["permissions", "status", "authorize", "revoke"],
                "description": "本人飞书授权动作：permissions 查看应用权限清单与后台开通链接，status 查询本人授权状态，authorize 私聊发送本人 OAuth 登录卡，revoke 撤销本地已存本人授权。"}},
                      "required": ["action"], "additionalProperties": False},
        hints=ToolModelHints(
            category="network",
            use_cases=(
                "查询飞书消息历史、会话列表或电子表格所需的精确权限清单与开发者后台一键申请链接（permissions/status）",
                "在私人助手会话中向当前用户发送本人飞书 OAuth 登录授权卡（authorize）或撤销已保存授权（revoke）",
            ),
            avoid_when=(
                "实际读写已共享飞书表格与云文档内容时用 feishu_cloud",
                "读取飞书群聊或本人可见会话消息历史时用 channel_messages",
            ),
            keywords=("飞书", "本人", "登录", "授权", "权限", "开发者后台", "OAuth"),
            examples=(
                '{"action":"permissions"}',
                '{"action":"status"}',
                '{"action":"authorize"}',
            ),
            default_deferred=True,
            deferred_summary="飞书权限清单及应用后台申请链接；本人授权状态/私发登录卡"))
    runtime_policy = ToolRuntimePolicy(effect_resolver=EffectResolverPolicy("mutating",
        by_parameter=(("action", (("status", "read_only"), ("permissions", "read_only"))),)), idempotency_policy=IdempotencyPolicy("operation"),
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
        # 授权未配好也必须让模型发现只读帮助；实际登录和撤销继续过原配置门。
        return ToolAvailability.ready()

    def _oauth_availability(self):
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
        if not isinstance(params, dict) or params.get("action") not in {"permissions", "status", "authorize", "revoke"} or set(params) - {"action", "__network_access_mode"}:
            return ToolHandlerOutcome(
                "feishu_auth",
                False,
                "本人授权动作无效。",
                error_code="TOOL_INVALID_ARGUMENTS",
                effect_outcome="not_started",
                result_envelope={"error_details": {"allowed_actions": ["permissions", "status", "authorize", "revoke"]}},
                next_action='仅支持 {"action": "permissions"|"status"|"authorize"|"revoke"}，请删除多余参数并修正 action 后再试。',
            )
        if params['action'] in {'permissions', 'status'}:
            guidance = authorization_guidance(self.agent)
            return ToolHandlerOutcome('feishu_auth', True, json.dumps(guidance, ensure_ascii=False),
                                      result_envelope={'authorization': guidance})
        readiness = self._oauth_availability()
        if not readiness.available:
            guidance = authorization_guidance(self.agent)
            is_group = False
            try:
                is_group = owner_identity_from_config(self.agent.config).owner_kind == "group"
            except (OSError, ValueError, TypeError, AttributeError):
                is_group = False
            available_entry, advice = _available_auth_entry_advice(self.agent, guidance, group_owner=is_group)
            details = {
                "reason_code": "GROUP_SESSION_OAUTH_UNSUPPORTED" if is_group else "OAUTH_DEPLOYMENT_NOT_CONFIGURED",
                "action": params["action"],
                "configuration_status": guidance.get("configuration_status", "not_configured"),
                "required_action": guidance.get("required_action", ""),
                "available_entry": available_entry,
            }
            return ToolHandlerOutcome(
                "feishu_auth",
                False,
                readiness.reason,
                error_code="TOOL_UNAVAILABLE",
                effect_outcome="not_started",
                result_envelope={"authorization": guidance, "error_details": details},
                next_action=advice,
            )
        try:
            if params["action"] == "authorize":
                try:
                    _assert_network(self.agent, invocation_mode=params.get("__network_access_mode"))
                except (OSError, ValueError, TypeError, KeyError):
                    guidance = authorization_guidance(self.agent)
                    return ToolHandlerOutcome(
                        "feishu_auth",
                        False,
                        "当前任务网络权限不可用，未发起本人授权。",
                        error_code="FEISHU_USER_AUTH_REQUIRED",
                        effect_outcome="not_started",
                        result_envelope={
                            "authorization": guidance,
                            "error_details": {
                                "reason_code": "NETWORK_DISABLED",
                                "action": "authorize",
                                "available_entry": 'feishu_auth(action="permissions")',
                            },
                        },
                        next_action='当前任务禁用外网请求（未发送登录卡）：可先调用只读 feishu_auth(action="permissions") 查看权限要求，或在允许网络的私人助手会话中发起 authorize。',
                    )
                dependency = request_oauth(self.agent, invocation_mode=params.get("__network_access_mode"))
                return ToolHandlerOutcome("feishu_auth", True, json.dumps(dependency, ensure_ascii=False),
                                           result_envelope={"input_dependency": dependency})
            return ToolHandlerOutcome("feishu_auth", True, json.dumps(
                oauth_status(self.agent, revoke=params["action"] == "revoke"), ensure_ascii=False))
        except (OSError, ValueError, TypeError, KeyError) as exc:
            guidance = authorization_guidance(self.agent)
            message, effect, advice, details = _classify_oauth_runtime_failure(self.agent, params["action"], exc, guidance)
            return ToolHandlerOutcome(
                "feishu_auth",
                False,
                message,
                error_code="FEISHU_USER_AUTH_REQUIRED",
                effect_outcome=effect,
                result_envelope={"authorization": guidance, "error_details": details},
                next_action=advice,
            )
