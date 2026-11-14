# LLM: 原工具注册表中的只读当前群入口；群号来自 owner，网络上界来自宿主绑定，模型不提供凭据或 HTTP 参数。
# 模块用途: 默认关闭并按需发现的飞书群历史工具，只执行 scope A 的有界 list。
from __future__ import annotations

import json

from ..adapter.feishu_message_history import (
    ChannelMessageHistoryError,
    HistoryLimits,
    current_history_chat,
    list_group_messages,
    validate_history_query,
)
from .models import (
    BaseTool,
    EffectResolverPolicy,
    TimeoutPolicy,
    ToolAvailability,
    ToolHandlerOutcome,
    ToolInputPolicy,
    ToolModelHints,
    ToolModelSpec,
    ToolRuntimePolicy,
    TrustedParameterBinding,
)


# LLM: Schema 不暴露 chat_id、凭据、URL 或写动作；参数与直接执行共用纯校验。
# 函数用途: 声明当前群有界历史与覆盖事实，保留默认折叠提示。
def _model_spec():
    return ToolModelSpec(name="channel_messages", description=(
        "只读飞书平台可见历史：默认current_group保留当前群范围；个人助手可显式selector=user用本人OAuth。"
        "先action=status查所需权限/本人授权入口，action=list_chats分页列本人p2p和群，再list指定chat_id。"
        "缺授权调用feishu_auth authorize，首次本人同意后宿主自动续期；不借其他用户或群的私人token。"
        "sender_ids按准确ID匹配，不用昵称或角色猜测。"
        "消息 create_time 返回毫秒；最多七天、五页、200条，正文每条最多2000字。"
        "coverage/has_more/next_cursor 明确部分结果与续读；不包含完整话题回复，不创建事件订阅或水位。"
        "机器人在群、应用可用范围和用户权限仍受飞书平台限制；不向用户索取秘密。"),
        input_schema={"type": "object", "additionalProperties": False,
            "required": ["action"], "properties": {
                "action": {"type": "string", "enum": ["list", "list_chats", "status"],
                    "description": "历史消息只读动作：status 查看当前身份所需飞书权限与授权入口，list_chats（仅 selector=user）分页列出本人可见会话，list 读取当前群或指定 chat_id 消息。"},
                "selector": {"type": "string", "enum": ["current_group", "user"],
                    "description": "默认current_group保持当前群范围；user仅本人私人助手OAuth身份。"},
                "chat_id": {"type": "string", "description": "仅user list使用本人可见会话的精确chat_id，先list_chats发现。"},
                "chat_types": {"type": "array", "items": {"type": "string", "enum": ["p2p", "group"]},
                    "description": "仅user list_chats，默认包含p2p和group，不以机器人身份枚举私人会话。"},
                "start_time": {"type": "integer", "minimum": 0, "description": "窗口起点，Unix秒。"},
                "end_time": {"type": "integer", "minimum": 1, "description": "窗口终点，Unix秒；跨度最多七天。"},
                "sender_ids": {"type": "array", "maxItems": HistoryLimits.MAX_SENDER_IDS,
                               "items": {"type": "string", "minLength": 1, "maxLength": 256},
                               "description": "可选发送者精确 ID 过滤列表（按平台用户/open_id 精确匹配，不接受昵称猜测）。"},
                "cursor": {"type": "string", "maxLength": HistoryLimits.MAX_CURSOR_CHARS,
                           "description": "原样使用同一窗口、过滤和当前群返回的 next_cursor。"},
                "limit": {"type": "integer", "minimum": 1, "maximum": HistoryLimits.MAX_MESSAGES,
                          "description": "单次返回的最大消息或会话条数（1 至 200）。"}}},
        hints=ToolModelHints(
            category="capability",
            use_cases=(
                "在飞书群会话中按时间窗口只读读取当前群消息历史（selector=current_group）",
                "在私人助手会话中以本人 OAuth 身份分页列出可见私聊/群会话（list_chats）并读取指定 chat_id 消息",
                "读取前通过 action=status 核查当前身份所需飞书权限范围与授权入口",
            ),
            avoid_when=(
                "向当前群或飞书会话主动发送出站消息时用 send_message（本工具仅只读拉取历史）",
                "检索 Agent 本地已保存的会话归档与对话上下文时用 session_search（本工具查飞书平台侧历史）",
                "发起本人飞书 OAuth 登录授权卡或撤销已存本人授权时用 feishu_auth",
            ),
            examples=(
                '{"action":"status"}',
                '{"action":"list","start_time":1700000000,"end_time":1700086400,"limit":50}',
                '{"action":"list_chats","selector":"user","limit":20}',
            ),
            default_deferred=True,
            deferred_summary="读取当前群或本人OAuth可见的私聊/群历史，列会话和查询授权要求",
            keywords=("飞书", "群聊", "历史", "消息"),
        ))


# LLM: 无网络/状态副作用；即使绕过注册表直接 execute，也拒绝注入群号、URL 或认证参数。
# 函数用途: 校验动作与公开查询形状，返回供原 adapter 使用的参数。
def _query(params):
    if not isinstance(params, dict):
        raise ChannelMessageHistoryError("TOOL_INVALID_ARGUMENTS")
    action, selector = params.get('action'), params.get('selector', 'current_group')
    if action not in {'list', 'list_chats', 'status'} or selector not in {'current_group', 'user'}:
        raise ChannelMessageHistoryError('TOOL_INVALID_ARGUMENTS')
    allowed = {'action', 'selector', '__network_access_mode'}
    if action == 'list':
        allowed |= {'start_time', 'end_time', 'sender_ids', 'cursor', 'limit'}
        if selector == 'user':
            allowed.add('chat_id')
            if not isinstance(params.get('chat_id'), str) or not params['chat_id']:
                raise ChannelMessageHistoryError('TOOL_INVALID_ARGUMENTS')
        query = {key: params[key] for key in ('start_time', 'end_time', 'sender_ids', 'cursor', 'limit') if key in params}
        if not {'start_time', 'end_time'} <= query.keys():
            raise ChannelMessageHistoryError('TOOL_INVALID_ARGUMENTS')
        validate_history_query(**query)
    elif action == 'list_chats':
        if selector != 'user':
            raise ChannelMessageHistoryError('TOOL_INVALID_ARGUMENTS')
        allowed |= {'cursor', 'limit', 'chat_types'}
        query = {key: params[key] for key in ('cursor', 'limit', 'chat_types') if key in params}
        from ..adapter.feishu_user_history import validate_user_chat_query
        validate_user_chat_query(**query)
    else:
        query = {}
    if set(params) - allowed:
        raise ChannelMessageHistoryError('TOOL_INVALID_ARGUMENTS')
    return action, selector, query


def _permission_facts(agent, selector, action):
    from ..user_space.feishu_authorization import permission_requirements
    scopes = ('im:chat:read',) if action == 'list_chats' else (
        'im:chat:readonly', 'im:message:readonly', 'im:message.p2p_msg:get_as_user', 'im:message.group_msg:get_as_user')
    if selector != 'user':
        scopes = ('im:message:readonly', 'im:message.group_msg')
    return permission_requirements(agent, required_scopes=scopes, identity='user' if selector == 'user' else 'bot',
        official_api_docs=('https://open.feishu.cn/document/server-docs/im-v1/message/list',
                          'https://github.com/larksuite/cli/blob/v1.0.98/skills/lark-im/references/lark-im-chat-list.md'))


_FAILURE_SPECS = {
    "TOOL_INVALID_ARGUMENTS": (
        "飞书历史查询参数不合法（未发起请求）：action/selector 组合、起止时间戳（不超过7天）或 user 模式必填 chat_id 不符合要求。",
        "invalid_query_parameters",
        '按 Schema 修正参数后再调用；当前群传 {"action":"list","start_time":...,"end_time":...}，个人模式先调用 {"action":"list_chats","selector":"user"} 获取精确 chat_id。',
    ),
    "NETWORK_DISABLED": (
        "当前执行上下文已禁用网络访问（__network_access_mode=disabled），未发起飞书历史请求。",
        "network_access_disabled",
        '当前无网运行边界不能拉取飞书消息；仅可调用本地只读 {"action":"status"}，或在允许网络的会话中查询。',
    ),
    "CHANNEL_MESSAGES_DISABLED": (
        "飞书消息历史读取能力（feishu_chat_history_enabled）当前处于关闭状态，未发起请求。",
        "feature_flag_disabled",
        "需先由管理员或当前 owner 开启 feishu_chat_history_enabled 配置开关，切勿原样重试。",
    ),
    "CHANNEL_MESSAGES_GROUP_REQUIRED": (
        "当前会话未绑定有效飞书群上下文，无法使用 selector=current_group 读取群历史。",
        "current_group_context_missing",
        '私人助手会话请显式传 selector="user"（先 list_chats 发现 chat_id），或切回已授权飞书群会话后使用 current_group。',
    ),
    "CHANNEL_MESSAGES_SCOPE_UNVERIFIED": (
        "当前会话身份与所选历史范围不匹配（群会话不能使用 selector=user，私人会话不能假借未绑定当前群）。",
        "identity_scope_mismatch",
        '群会话请使用默认 selector="current_group"；私人助手会话请使用 selector="user" 并先调用 {"action":"status","selector":"user"} 核验身份。',
    ),
    "CHANNEL_MESSAGES_CHAT_UNVERIFIED": (
        "无法核验当前会话对应的飞书群 chat_id，未读取群消息历史。",
        "current_group_chat_unverified",
        '确认当前对话发生在已接入飞书群内，或在私人助手会话改用 selector="user" 配合 list_chats。',
    ),
    "FEISHU_USER_AUTH_REQUIRED": (
        "当前用户本人飞书 OAuth 授权缺失、已撤销或配置发生变更，未读取本人可见会话历史。",
        "feishu_user_oauth_required",
        '请先调用 feishu_auth(action="authorize") 完成本人飞书登录授权，或用 channel_messages(action="status", selector="user") 查看所需权限。',
    ),
    "FEISHU_PROVIDER_SCOPE_REQUIRED": (
        "飞书应用缺少本次历史查询所需的平台权限范围（scope），请求被飞书平台拒绝。",
        "feishu_provider_scope_missing",
        '请查看返回 authorization.required_scopes 或调用 feishu_auth(action="permissions") 获取开发者后台权限开通链接，开通后再查询。',
    ),
    "CHANNEL_MESSAGES_PERMISSION_DENIED": (
        "飞书平台拒绝当前机器人或本人身份访问目标会话消息（不在会话内或应用可用范围受限）。",
        "feishu_chat_access_denied",
        '先调用 channel_messages(action="status") 核对应用权限并确认机器人/本人仍在目标会话中，不要盲目重复请求。',
    ),
    "CHANNEL_MESSAGES_RATE_LIMITED": (
        "飞书平台历史消息接口触发频控限制，本次只读拉取未完成。",
        "feishu_rate_limited",
        "缩小时间窗口或降低 limit 后稍候再试，禁止连续高频重放同一查询。",
    ),
}


def _diagnose_unavailable_cause(agent, selector: str, exc_type: str) -> tuple[str, str, str]:
    if agent is not None:
        home = getattr(agent, "home_paths", None)
        owner_kind = getattr(home, "owner_kind", "")
        owner_provider = getattr(home, "owner_provider", "")
        if selector == "user" and owner_kind == "group":
            return _FAILURE_SPECS["CHANNEL_MESSAGES_SCOPE_UNVERIFIED"]
        if selector == "current_group" and owner_kind and owner_kind != "group":
            return (
                "当前会话属于个人助手而非飞书群上下文，无法使用默认 selector=current_group 读取当前群。",
                "private_session_missing_current_group",
                '在私人助手会话中请显式指定 selector="user"（先 list_chats 获取 chat_id），或切回目标飞书群会话。',
            )
        if owner_kind != "group" and owner_provider and owner_provider not in {"feishu", "local"}:
            return (
                f"当前会话渠道（{owner_provider}）不支持飞书平台历史读取接口。",
                "unsupported_owner_provider",
                "仅飞书群或绑定飞书身份的助手会话可调用 channel_messages；若需检索本地对话记录请改用 session_search。",
            )
        try:
            from ..adapter.feishu_message_history import (
                capability_config_for_agent,
                deployment_capability_config,
            )

            if home is not None and (
                not capability_config_for_agent(agent).feishu_chat_history_enabled
                or not deployment_capability_config(home).feishu_chat_history_enabled
            ):
                return _FAILURE_SPECS["CHANNEL_MESSAGES_DISABLED"]
        except (ChannelMessageHistoryError, OSError, AttributeError, TypeError, ValueError):
            pass
    suffix = f"（{exc_type}）" if exc_type else ""
    return (
        f"飞书历史读取前置状态或底层通道暂不可用{suffix}，未返回消息记录。",
        "channel_messages_service_unavailable",
        '请先调用 channel_messages(action="status") 检查当前身份的权限配置与可用 selector，确认就绪后再查询。',
    )


# LLM: 输出仅单一定位原因和净化机器码，绝不拼接供应商自由异常、请求 URL 或认证材料。
# 函数用途: 构造按结构化错误码与就绪状态精确定位的无副作用安全失败回执。
def _failure(code, *, agent=None, params=None, exc_type=""):
    selector = params.get("selector", "current_group") if isinstance(params, dict) else "current_group"
    action = params.get("action", "") if isinstance(params, dict) else ""
    if code in _FAILURE_SPECS:
        message, cause, advice = _FAILURE_SPECS[code]
    elif code == "CHANNEL_MESSAGES_UNAVAILABLE":
        message, cause, advice = _diagnose_unavailable_cause(agent, str(selector), exc_type)
    else:
        message = f"飞书历史查询未通过前置或平台校验（错误码 {code}）。"
        cause = code.lower()
        advice = '请先调用 channel_messages(action="status") 核查该错误码对应的权限或范围要求，切勿原样盲目重试。'
    details = {
        "cause": cause,
        "error_code": code,
        "selector": str(selector),
        **({"action": str(action)} if action else {}),
        **({"exception_type": exc_type} if exc_type else {}),
    }
    return ToolHandlerOutcome(
        "channel_messages",
        False,
        message,
        error_code=code,
        effect_outcome="not_started",
        result_envelope={"error_details": details},
        next_action=advice,
    )


# LLM: 每次实际执行重新检查开关和实时群证明，不能因注册时开启就永久保留访问权。
# 类用途: 将宿主当前群绑定到冻结的有界消息 reader。
class ChannelMessagesTool(BaseTool):
    model_spec = _model_spec()
    runtime_policy = ToolRuntimePolicy(effect_resolver=EffectResolverPolicy("read_only"),
        timeout_policy=TimeoutPolicy(seconds=35),
        input_policy=ToolInputPolicy(internal_parameters=("__network_access_mode",), trusted_parameter_bindings=(
            ("__network_access_mode", TrustedParameterBinding(source_refs=("write_boundary.network_access_mode",),
                                                              authority="host_authoritative")),)))

    # LLM: 注册不读凭据、不初始化连接、不触发网络或文件写入。
    # 函数用途: 保存原宿主 Agent。
    def __init__(self, agent):
        self.agent = agent

    # LLM: 仅本地开关、owner 和网络上界检查；实际证明留到执行，目录检查不能读取群消息。
    # 函数用途: 对关闭、私人 owner 或网络关闭显示不可用。
    def availability(self):
        try:
            home = self.agent.home_paths
            if getattr(home, 'owner_kind', '') == 'group':
                current_history_chat(self.agent)
            else:
                from ..adapter.feishu_message_history import (
                    capability_config_for_agent,
                    deployment_capability_config,
                )
                if (getattr(home, 'owner_provider', '') not in {'feishu', 'local'}
                        or not capability_config_for_agent(self.agent).feishu_chat_history_enabled
                        or not deployment_capability_config(home).feishu_chat_history_enabled):
                    raise ChannelMessageHistoryError('CHANNEL_MESSAGES_DISABLED')
        except (ChannelMessageHistoryError, OSError, AttributeError, TypeError, ValueError):
            return ToolAvailability.unavailable("当前owner的飞书历史入口未启用、供应商不支持或身份范围不可核验。")
        return ToolAvailability.ready()

    # LLM: 调用前只做纯参数校验，不能提前读群证明或消息。
    # 函数用途: 在 handler 之前拒绝非法或越界形状。
    def validate_invocation(self, params, context):
        try:
            _query(params)
        except ChannelMessageHistoryError:
            return _failure("TOOL_INVALID_ARGUMENTS", agent=self.agent, params=params)
        return None

    # LLM: 宿主继承的 disabled 先于任何授权 HTTP；直接调用同样严格检查形状，授权失败丢弃结果。
    # 函数用途: 读取当前群窗口，返回正文及真实部分覆盖事实。
    def execute(self, params):
        try:
            action, selector, query = _query(params)
            if action == 'status':
                facts = _permission_facts(self.agent, selector, 'list')
                facts['supported_selectors'] = ['current_group'] if getattr(self.agent.home_paths, 'owner_kind', '') == 'group' else ['user']
                facts['user_identity_context'] = 'private_owner_only'
                if selector == 'user':
                    facts['chat_discovery'] = _permission_facts(self.agent, selector, 'list_chats')
                return ToolHandlerOutcome('channel_messages', True, json.dumps(facts, ensure_ascii=False),
                    result_envelope={'authorization': facts})
            if params.get("__network_access_mode") == "disabled":
                return _failure("NETWORK_DISABLED", agent=self.agent, params=params)
            if selector == 'user':
                from ..adapter.feishu_user_history import list_user_chats, list_user_messages
                result = list_user_chats(self.agent, **query) if action == 'list_chats' else list_user_messages(
                    self.agent, params['chat_id'], **query)
            else:
                chat_id = current_history_chat(self.agent)
                result = list_group_messages(self.agent, chat_id, **query)
            return ToolHandlerOutcome("channel_messages", True, json.dumps(result, ensure_ascii=False, allow_nan=False),
                                      result_envelope={"channel_messages": result})
        except ChannelMessageHistoryError as exc:
            result = _failure(exc.error_code, agent=self.agent, params=params)
            if isinstance(params, dict) and params.get('selector') == 'user':
                facts = _permission_facts(self.agent, 'user', params.get('action', 'list'))
                envelope = dict(result.result_envelope or {})
                envelope['authorization'] = facts
                result.result_envelope = envelope
                result.output = json.dumps(
                    {
                        'error_code': exc.error_code,
                        'message': result.output,
                        'authorization': facts,
                        'error_details': envelope.get('error_details', {}),
                    },
                    ensure_ascii=False,
                )
            return result
        except (OSError, AttributeError, TypeError, ValueError) as exc:
            return _failure("CHANNEL_MESSAGES_UNAVAILABLE", agent=self.agent, params=params, exc_type=type(exc).__name__)
