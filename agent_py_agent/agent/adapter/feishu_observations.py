# LLM: provider 的机器人和未知来源必须先分流，不能经过人类命令、会话解锁或 HostChannelActor；内容仅为不可信资料。
# 模块用途: 归一化飞书机器人观察并交原持久入站消费者，不执行条件匹配或产生用户权限。
from __future__ import annotations

from .protocol import FeishuEventDisposition, _feishu_timestamp


# LLM: 观察和实时事实只扩展原 adapter 合同，不承担执行、权限或第二存储。
# 类用途: 为实际飞书适配器提供群事实查询和观察消费者接线。
class FeishuObservationMixin:
    # LLM: 只复用当前 REST adapter 认证，不依赖另一进程的 WS 或新增成员权限缓存。
    # 函数用途: 向原群授权门提供官方当前角色和完整成员证明。
    def group_authority_snapshot(self, chat_id: str) -> dict:
        from .feishu_provider_facts import adapter_group_authority_snapshot
        return adapter_group_authority_snapshot(self, chat_id)

    # LLM: 同一REST成员接口只给完整成员/姓名投影，不含群主或机器人证明，不能替代原授权或代删检查。
    # 函数用途: 为当前群缺名创建者提供已有姓名，不申请新scope或启动WS。
    def group_member_snapshot(self, chat_id: str) -> dict:
        from .feishu_provider_facts import adapter_group_member_snapshot
        return adapter_group_member_snapshot(self, chat_id)

    # LLM: callback 来自唯一宿主 manager，bot观察不进入人类命令、actor 或 /ask 入口。
    # 函数用途: 注册结构化机器人观察的持久入站消费者。
    def on_observation(self, callback) -> None:
        self._observation_callback = callback


# LLM: app_id 来自已认证接收实例并与官方 header 对照；保留 raw bot/open_id，后续只经同消息 Get 得 canonical app_id。
# 函数用途: 将群机器人事件转换成明确类型的观察；私聊机器人、自机器人和缺身份来源拒绝。
def admit_bot_observation(adapter, payload, bot_open_id):
    header, event = payload.get('header') or {}, payload.get('event') or {}
    message, sender = event.get('message') or {}, event.get('sender') or {}
    sender_id = sender.get('sender_id') or {}
    app_id = header.get('app_id')
    tenant_key = header.get('tenant_key')
    source = sender_id.get('open_id')
    if (message.get('chat_type') != 'group' or sender.get('sender_type') not in {'bot', 'app'}
            or app_id != adapter.app_id or not all(isinstance(value, str) and value for value in
                (tenant_key, source, message.get('message_id'), message.get('chat_id')))):
        return FeishuEventDisposition('ignored', 'FEISHU_OBSERVATION_INVALID')
    if bot_open_id and source == bot_open_id:
        return FeishuEventDisposition('ignored', 'FEISHU_BOT_LOOP_SUPPRESSED')
    callback = getattr(adapter, '_observation_callback', None)
    if callback is None:
        return FeishuEventDisposition('retryable', 'FEISHU_OBSERVATION_UNAVAILABLE')
    raw = message.get('content')
    observation = {'channel': 'feishu', 'event_type': 'message', 'app_id': app_id, 'tenant_key': tenant_key,
        'chat_id': message['chat_id'], 'message_id': message['message_id'], 'sender_type': sender['sender_type'],
        'sender_id': source, 'sender_id_type': 'open_id', 'created_at': _feishu_timestamp(message.get('create_time')),
        'content': raw if isinstance(raw, str) else '{}', 'message_type': str(message.get('message_type') or message.get('msg_type') or ''),
        'parent_id': str(message.get('parent_id') or ''), 'root_id': str(message.get('root_id') or ''),
        'thread_id': str(message.get('thread_id') or '')}
    if callback(observation) is False:
        return FeishuEventDisposition('retryable', 'FEISHU_OBSERVATION_UNAVAILABLE')
    return FeishuEventDisposition('accepted')
