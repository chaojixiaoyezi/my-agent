# LLM: 仅原已验证 SDK/webhook 事件可调用；事件只更新原 group_authorization 失效/邀请事实，不授予成员能力。
# 模块用途: 把官方进退群事件归一到原群授权策略账，当前角色仍每次读取官方快照。
from __future__ import annotations

from pathlib import Path
from urllib.parse import quote

from .feishu_provider_facts import FeishuProviderFactError, _read
from .protocol import FeishuEventDisposition

GROUP_EVENT_TYPES = frozenset({'im.chat.member.bot.added_v1', 'im.chat.member.bot.deleted_v1',
    'im.chat.member.user.added_v1', 'im.chat.member.user.deleted_v1'})


# LLM: 只复制白名单身份字段，不序列化整个 SDK 对象、token、名字或群正文。
# 函数用途: 为原 webhook 共用入口构造官方群成员事件结构。
def lark_group_event_payload(data):
    header, event = getattr(data, 'header', None), getattr(data, 'event', None)
    operator = getattr(event, 'operator_id', None)
    return {'header': {key: str(getattr(header, key, '') or '') for key in
        ('event_id', 'event_type', 'create_time', 'app_id', 'tenant_key')},
        'event': {'chat_id': str(getattr(event, 'chat_id', '') or ''),
            'operator_id': {'open_id': str(getattr(operator, 'open_id', '') or '')},
            'users': [{'user_id': {'open_id': str(getattr(getattr(row, 'user_id', None), 'open_id', '') or '')}}
                for row in (getattr(event, 'users', None) or [])]}}


# LLM: app_id 必须等于当前接收实例，tenant 与 snapshot 同源取 chat 元数据；operator_tenant_key 不是授权namespace。
# 函数用途: 写入原授权账的可信群事件；查询/写入失败保持失败 ACK，禁止把未知事实算已受理。
def admit_group_provider_event(adapter, payload):
    from ..user_space.group_authorization import (
        GroupProviderEvent,
        record_group_provider_event,
        record_group_provider_invalidation,
    )

    header, event = payload.get('header') or {}, payload.get('event') or {}
    if header.get('event_type') not in GROUP_EVENT_TYPES or header.get('app_id') != adapter.app_id or not adapter.my_agent_home:
        return FeishuEventDisposition('ignored', 'FEISHU_GROUP_EVENT_INVALID')
    chat_id = event.get('chat_id')
    try:
        if not isinstance(chat_id, str) or not chat_id or not header.get('event_id'):
            raise ValueError('missing event scope')
        created_at = header.get('create_time')
        if not isinstance(created_at, str) or not created_at.isascii() or not created_at.isdecimal():
            raise ValueError('invalid event time')
        operator = (event.get('operator_id') or {}).get('open_id') or ''
        users = event.get('users') or []
        if not isinstance(users, list):
            raise ValueError('invalid members')
        members = tuple((row.get('user_id') or {}).get('open_id') for row in users if isinstance(row, dict))
        if len(members) != len(users) or any(not isinstance(item, str) or not item for item in members):
            raise ValueError('unverified member identity')
        if '.user.' in header['event_type'] and not members:
            raise ValueError('missing member identity')
        record = GroupProviderEvent(provider='feishu', chat_id=chat_id, app_id=adapter.app_id, tenant_key='',
            event_id=header['event_id'], event_type=header['event_type'], created_at=int(created_at), operator_id=operator, member_ids=members)
        try:
            token = adapter._get_tenant_access_token()
            if not token:
                raise FeishuProviderFactError('FEISHU_PROVIDER_AUTH_UNAVAILABLE')
            info = _read(token, '/im/v1/chats/' + quote(chat_id, safe='') + '?user_id_type=open_id')
            tenant = info.get('tenant_key')
            if not isinstance(tenant, str) or not tenant:
                raise ValueError('missing group tenant')
        except (OSError, RuntimeError, TypeError, ValueError):
            if header['event_type'].endswith('deleted_v1'):
                record_group_provider_invalidation(Path(adapter.my_agent_home), record)
                return FeishuEventDisposition('accepted')
            raise
        from dataclasses import replace
        record_group_provider_event(Path(adapter.my_agent_home), replace(record, tenant_key=tenant))
    except (OSError, RuntimeError, TypeError, ValueError, AttributeError):
        return FeishuEventDisposition('retryable', 'FEISHU_GROUP_EVENT_UNAVAILABLE')
    return FeishuEventDisposition('accepted')
