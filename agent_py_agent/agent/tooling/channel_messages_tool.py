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
        "只读当前飞书群在指定秒级时间窗口内的平台可见 chat 历史。sender_ids 按准确 ID 匹配，不用昵称或角色猜测。"
        "消息 create_time 返回毫秒；最多七天、五页、200条，正文每条最多2000字。"
        "coverage/has_more/next_cursor 明确部分结果与续读；不包含完整话题回复，不创建事件订阅或水位。"
        "凭据与群号由宿主绑定，权限不足直接拒绝，不向用户索取秘密。"),
        input_schema={"type": "object", "additionalProperties": False,
            "required": ["action", "start_time", "end_time"], "properties": {
                "action": {"type": "string", "enum": ["list"]},
                "start_time": {"type": "integer", "minimum": 0, "description": "窗口起点，Unix秒。"},
                "end_time": {"type": "integer", "minimum": 1, "description": "窗口终点，Unix秒；跨度最多七天。"},
                "sender_ids": {"type": "array", "maxItems": HistoryLimits.MAX_SENDER_IDS,
                               "items": {"type": "string", "minLength": 1, "maxLength": 256}},
                "cursor": {"type": "string", "maxLength": HistoryLimits.MAX_CURSOR_CHARS,
                           "description": "原样使用同一窗口、过滤和当前群返回的 next_cursor。"},
                "limit": {"type": "integer", "minimum": 1, "maximum": HistoryLimits.MAX_MESSAGES}}},
        hints=ToolModelHints(category="capability", default_deferred=True,
                             deferred_summary="读取当前飞书群的有界历史消息", keywords=("飞书", "群聊", "历史", "消息")))


# LLM: 无网络/状态副作用；即使绕过注册表直接 execute，也拒绝注入群号、URL 或认证参数。
# 函数用途: 校验动作与公开查询形状，返回供原 adapter 使用的参数。
def _query(params):
    if (not isinstance(params, dict) or params.get("action") != "list"
            or set(params) - {"action", "start_time", "end_time", "sender_ids", "cursor", "limit", "__network_access_mode"}):
        raise ChannelMessageHistoryError("TOOL_INVALID_ARGUMENTS")
    query = {key: params[key] for key in ("start_time", "end_time", "sender_ids", "cursor", "limit") if key in params}
    if not {"start_time", "end_time"} <= query.keys():
        raise ChannelMessageHistoryError("TOOL_INVALID_ARGUMENTS")
    validate_history_query(**query)
    return query


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
            current_history_chat(self.agent)
        except ChannelMessageHistoryError:
            return ToolAvailability.unavailable("当前 owner 的飞书群历史未启用或不可核验。")
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
            query = _query(params)
            if params.get("__network_access_mode") == "disabled":
                return _failure("NETWORK_DISABLED")
            chat_id = current_history_chat(self.agent)
            result = list_group_messages(self.agent, chat_id, **query)
            return ToolHandlerOutcome("channel_messages", True, json.dumps(result, ensure_ascii=False, allow_nan=False),
                                      result_envelope={"channel_messages": result})
        except ChannelMessageHistoryError as exc:
            return _failure(exc.error_code)
        except (OSError, AttributeError, TypeError, ValueError):
            return _failure("CHANNEL_MESSAGES_UNAVAILABLE")
