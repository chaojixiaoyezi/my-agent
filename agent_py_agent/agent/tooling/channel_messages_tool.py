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
                "action": {"type": "string", "enum": ["list", "list_chats", "status"]},
                "selector": {"type": "string", "enum": ["current_group", "user"],
                    "description": "默认current_group保持当前群范围；user仅本人私人助手OAuth身份。"},
                "chat_id": {"type": "string", "description": "仅user list使用本人可见会话的精确chat_id，先list_chats发现。"},
                "chat_types": {"type": "array", "items": {"type": "string", "enum": ["p2p", "group"]},
                    "description": "仅user list_chats，默认包含p2p和group，不以机器人身份枚举私人会话。"},
                "start_time": {"type": "integer", "minimum": 0, "description": "窗口起点，Unix秒。"},
                "end_time": {"type": "integer", "minimum": 1, "description": "窗口终点，Unix秒；跨度最多七天。"},
                "sender_ids": {"type": "array", "maxItems": HistoryLimits.MAX_SENDER_IDS,
                               "items": {"type": "string", "minLength": 1, "maxLength": 256}},
                "cursor": {"type": "string", "maxLength": HistoryLimits.MAX_CURSOR_CHARS,
                           "description": "原样使用同一窗口、过滤和当前群返回的 next_cursor。"},
                "limit": {"type": "integer", "minimum": 1, "maximum": HistoryLimits.MAX_MESSAGES}}},
        hints=ToolModelHints(category="capability", default_deferred=True,
                             deferred_summary="读取当前群或本人OAuth可见的私聊/群历史，列会话和查询授权要求", keywords=("飞书", "群聊", "历史", "消息")))


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


# LLM: 输出仅固定说明和净化机器码，绝不拼接供应商自由异常、请求 URL 或认证材料。
# 函数用途: 构造无私有消息的安全失败。
def _failure(code):
    return ToolHandlerOutcome("channel_messages", False,
        "未读取当前群历史：请检查查询参数、功能开关、当前群归属、网络和飞书权限。", error_code=code)


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
            return _failure("TOOL_INVALID_ARGUMENTS")
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
                return _failure("NETWORK_DISABLED")
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
            result = _failure(exc.error_code)
            if isinstance(params, dict) and params.get('selector') == 'user':
                facts = _permission_facts(self.agent, 'user', params.get('action', 'list'))
                result.result_envelope = {'authorization': facts}
                result.output = json.dumps({'error_code': exc.error_code, 'authorization': facts}, ensure_ascii=False)
            return result
        except (OSError, AttributeError, TypeError, ValueError):
            return _failure("CHANNEL_MESSAGES_UNAVAILABLE")
