# LLM: 只通过宿主原 Delivery registry 的真实 REST adapter 读取平台事实；不寻找另一进程的 WS 实例，不保存权限账或接受模型 token。
# 模块用途: 为群授权和事件订阅共用当前群角色、完整成员及精确来源消息证明，不启动连接或申请权限。
from __future__ import annotations

import time
from urllib.parse import quote, urlencode


# LLM: code 仅来自供应商结构化响应，响应正文和秘密不得成为异常消息。
# 类用途: 给调用方区分缺权限、不可核验和暂态宿主故障。
class FeishuProviderFactError(RuntimeError):
    # LLM: 原因码由本模块常量选择，绝不把平台自由文本传回用户或日志。
    # 函数用途: 保存有界诊断与供应商整数码，不改运行状态。
    def __init__(self, reason_code, provider_code=None):
        self.reason_code, self.provider_code = reason_code, provider_code
        super().__init__(reason_code)


# LLM: 仅复用实际宿主 REST factory；缺失时拒绝，不读 desktop.yaml，不初始化 WS 或跨 owner 凭据。
# 函数用途: 取得原认证适配器及 tenant token，秘密仅保留于本次调用栈。
def _provider(agent):
    try:
        adapter = agent.delivery_service.registry.adapter_for('feishu')
        token = adapter._get_tenant_access_token() if adapter is not None else None
        if not token:
            raise FeishuProviderFactError('FEISHU_PROVIDER_AUTH_UNAVAILABLE')
        return adapter, token
    except (AttributeError, TypeError) as exc:
        raise FeishuProviderFactError('FEISHU_PROVIDER_UNAVAILABLE') from exc


# LLM: path 只能由本模块准确资源构造，原 authenticated HTTP 保持无重定向；不允许客户端提交任意 URL。
# 函数用途: 复用原 REST 请求和认证，提取成功 data 或明确报缺权限，不修改平台授权。
def _read(token, path, *, timeout=5.0):
    from .feishu import _feishu_msg_api

    response = _feishu_msg_api('GET', path, {}, token, timeout=timeout)
    if not isinstance(response, dict) or type(response.get('code')) is not int:
        raise FeishuProviderFactError('FEISHU_PROVIDER_RESPONSE_INVALID')
    code = response['code']
    if code != 0:
        reason = 'FEISHU_PROVIDER_SCOPE_REQUIRED' if code in {99991663, 99991668, 99991672, 99991679, 230001} else 'FEISHU_PROVIDER_READ_REJECTED'
        raise FeishuProviderFactError(reason, code)
    data = response.get('data')
    if not isinstance(data, dict):
        raise FeishuProviderFactError('FEISHU_PROVIDER_RESPONSE_INVALID')
    return data


# LLM: 群与消息均是准确不透明 ID，编码后只访问单个资源；引用正文和 sender 不能变成 actor。
# 函数用途: 读取并核验一条当前群原消息，拒绝缺失、删除、跨群或身份不明确的结果。
def _message(token, chat_id, message_id):
    data = _read(token, '/im/v1/messages/' + quote(message_id, safe=''))
    items = data.get('items')
    if not isinstance(items, list) or len(items) != 1 or not isinstance(items[0], dict):
        raise FeishuProviderFactError('FEISHU_SOURCE_MESSAGE_UNVERIFIED')
    row = items[0]
    if row.get('message_id') != message_id or row.get('chat_id') != chat_id or row.get('deleted') is True:
        raise FeishuProviderFactError('FEISHU_SOURCE_MESSAGE_SCOPE_MISMATCH')
    return row


# LLM: Get 的 app/app_id 与事件的 bot/open_id 不是等值关系；只用同一 message_id 的平台证明得到 canonical app_id。
# 函数用途: 按用户引用的准确原消息定位其他机器人，保留来源证明供原订阅登记。
def resolve_bot_source(agent, chat_id, source_message_id):
    if not all(isinstance(value, str) and value for value in (chat_id, source_message_id)):
        raise FeishuProviderFactError('FEISHU_SOURCE_MESSAGE_REQUIRED')
    adapter, token = _provider(agent)
    row = _message(token, chat_id, source_message_id)
    sender = row.get('sender') or {}
    if (sender.get('sender_type') != 'app' or sender.get('id_type') != 'app_id'
            or not isinstance(sender.get('id'), str) or not sender['id'] or sender['id'] == adapter.app_id):
        raise FeishuProviderFactError('FEISHU_OTHER_BOT_SOURCE_REQUIRED')
    return {'sender_id': sender['id'], 'sender_id_type': 'app_id', 'source_message_id': source_message_id,
        'sender_tenant_key': str(sender.get('tenant_key') or ''), 'chat_id': chat_id, 'app_id': adapter.app_id}


# LLM: token 是 tenant token，is_in_chat 只证明本机器人；当前人类成员必须完整分页，群主/管理员只读显式角色字段。
# 函数用途: 取得实时群授权快照，不缓存或保存成员权限，部分分页或身份类型不明时拒绝。
def group_authority_snapshot(agent, chat_id):
    adapter, token = _provider(agent)
    return _group_authority_snapshot(adapter, token, chat_id)


# LLM: 供实际 FeishuAdapter 方法使用同一读取实现，不能构造另一实例或连接。
# 函数用途: 使用原 adapter 的认证读取实时群快照。
def adapter_group_authority_snapshot(adapter, chat_id):
    token = adapter._get_tenant_access_token()
    if not token:
        raise FeishuProviderFactError('FEISHU_PROVIDER_AUTH_UNAVAILABLE')
    return _group_authority_snapshot(adapter, token, chat_id)


# LLM: 只有全部官方分页成功才能返回 members_complete，token自己在群不等于任意人被授权。
# 函数用途: 共用实际群角色和成员读取，不保存第二权限账。
def _group_authority_snapshot(adapter, token, chat_id):
    path = '/im/v1/chats/' + quote(chat_id, safe='')
    info = _read(token, path + '?user_id_type=open_id')
    own = _read(token, path + '/members/is_in_chat')
    if own.get('is_in_chat') is not True or info.get('chat_mode') not in {'group', 'topic'}:
        raise FeishuProviderFactError('FEISHU_BOT_GROUP_MEMBERSHIP_REQUIRED')
    members, seen, cursor, deadline = set(), set(), '', time.monotonic() + 5
    for _page in range(10):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        params = {'member_id_type': 'open_id', 'page_size': 100}
        if cursor:
            params['page_token'] = cursor
        data = _read(token, path + '/members?' + urlencode(params), timeout=remaining)
        rows = data.get('items')
        if not isinstance(rows, list) or any(not isinstance(row, dict) or row.get('member_id_type') != 'open_id'
                or not isinstance(row.get('member_id'), str) or not row['member_id'] for row in rows):
            break
        members.update(row['member_id'] for row in rows)
        if data.get('has_more') is False:
            owner = str(info.get('owner_id') or '') if info.get('owner_id_type') == 'open_id' else ''
            return {'provider': 'feishu', 'channel': 'feishu', 'app_id': adapter.app_id, 'chat_id': chat_id,
                'tenant_key': str(info.get('tenant_key') or ''), 'owner_id': owner, 'owner_id_type': 'open_id',
                'member_ids': sorted(members), 'members_complete': True,
                'bot_is_member': True, 'observed_at': time.time()}
        cursor = data.get('page_token')
        if not isinstance(cursor, str) or not cursor or cursor in seen:
            break
        seen.add(cursor)
    raise FeishuProviderFactError('FEISHU_GROUP_MEMBERS_INCOMPLETE')


# LLM: 只核真实 receiver app、原消息 sender 和同群关系；外部 bot 不是 human actor，引用链也不产生权限。
# 函数用途: 将原事件证据转换为可订阅的 app_id 身份，并拒绝对自己出站消息的可证回环。
def canonical_bot_observation(agent, event):
    adapter, token = _provider(agent)
    if event.get('app_id') != adapter.app_id or event.get('sender_type') not in {'bot', 'app'}:
        raise FeishuProviderFactError('FEISHU_OBSERVATION_SCOPE_MISMATCH')
    chat = _read(token, '/im/v1/chats/' + quote(event['chat_id'], safe='') + '?user_id_type=open_id')
    if chat.get('chat_mode') not in {'group', 'topic'} or not isinstance(chat.get('tenant_key'), str) or not chat['tenant_key']:
        raise FeishuProviderFactError('FEISHU_OBSERVATION_SCOPE_MISMATCH')
    row = _message(token, event['chat_id'], event['message_id'])
    sender = row.get('sender') or {}
    if sender.get('sender_type') != 'app' or sender.get('id_type') != 'app_id' or not sender.get('id'):
        raise FeishuProviderFactError('FEISHU_OTHER_BOT_SOURCE_REQUIRED')
    if sender['id'] == adapter.app_id:
        raise FeishuProviderFactError('FEISHU_BOT_LOOP_SUPPRESSED')
    for message_id in set(filter(None, [row.get('root_id'), row.get('parent_id')])):
        ancestor = _message(token, event['chat_id'], message_id)
        source = ancestor.get('sender') or {}
        if source.get('sender_type') == 'app' and source.get('id_type') == 'app_id' and source.get('id') == adapter.app_id:
            raise FeishuProviderFactError('FEISHU_BOT_LOOP_SUPPRESSED')
    return {**event, 'provider_tenant_key': event['tenant_key'], 'tenant_key': chat['tenant_key'],
        'sender_type': 'bot', 'sender_id': sender['id'], 'sender_id_type': 'app_id',
        'sender_tenant_key': str(sender.get('tenant_key') or '')}
