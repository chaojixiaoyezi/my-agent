# LLM: 只通过宿主原 Delivery registry 的真实 REST adapter 读取平台事实；不寻找另一进程的 WS 实例，不保存权限账或接受模型 token。
# 模块用途: 为群授权和事件订阅共用角色、完整成员及可选姓名投影和原消息证明，不启动连接或申请权限；姓名不参与授权。
from __future__ import annotations

import time
from urllib.error import HTTPError
from urllib.parse import quote, urlencode

_FACT_REASON_CODES = frozenset({
    'FEISHU_PROVIDER_AUTH_UNAVAILABLE', 'FEISHU_PROVIDER_UNAVAILABLE', 'FEISHU_PROVIDER_RESPONSE_INVALID',
    'FEISHU_PROVIDER_SCOPE_REQUIRED', 'FEISHU_PROVIDER_PERMISSION_REQUIRED', 'FEISHU_PROVIDER_READ_REJECTED',
    'FEISHU_PROVIDER_HTTP_REJECTED', 'FEISHU_PROVIDER_TRANSPORT_UNAVAILABLE', 'FEISHU_SOURCE_MESSAGE_UNVERIFIED',
    'FEISHU_SOURCE_MESSAGE_SCOPE_MISMATCH', 'FEISHU_SOURCE_MESSAGE_REQUIRED', 'FEISHU_OTHER_BOT_SOURCE_REQUIRED',
    'FEISHU_BOT_GROUP_MEMBERSHIP_REQUIRED', 'FEISHU_GROUP_OWNER_UNVERIFIED', 'FEISHU_GROUP_TENANT_UNVERIFIED',
    'FEISHU_GROUP_MEMBERS_INCOMPLETE', 'FEISHU_OBSERVATION_SCOPE_MISMATCH', 'FEISHU_BOT_LOOP_SUPPRESSED',
})
_FACT_STAGES = frozenset({'auth', 'registry', 'chat_info', 'bot_membership', 'members', 'message', 'response_validation'})


# LLM: code 仅来自供应商结构化响应，响应正文和秘密不得成为异常消息。
# 类用途: 给调用方区分缺权限、不可核验和暂态宿主故障。
class FeishuProviderFactError(RuntimeError):
    # LLM: reason由宿主常量选择，stage仅固定阶段，provider_code仅准确整数；自由正文/URL/身份不得进入诊断。
    # 函数用途: 保存可安全投影的原因码、供应商整数码和查询阶段，不改运行状态。
    def __init__(self, reason_code, provider_code=None, *, stage=''):
        self.reason_code = reason_code if type(reason_code) is str and reason_code in _FACT_REASON_CODES else 'FEISHU_PROVIDER_UNAVAILABLE'
        self.provider_code = provider_code if type(provider_code) is int else None
        self.stage = stage if type(stage) is str and stage in _FACT_STAGES else ''
        super().__init__(self.reason_code)

    # LLM: 只在此处投影固定诊断合同；调用方不能复用异常正文、URL或链，实例字段被更改也须再次净化。
    # 函数用途: 向群控制等宿主边界提供三个可公开字段，不打印或持久化平台内容。
    def safe_diagnostics(self):
        return {'reason_code': self.reason_code if type(self.reason_code) is str and self.reason_code in _FACT_REASON_CODES else 'FEISHU_PROVIDER_UNAVAILABLE',
            'provider_code': self.provider_code if type(self.provider_code) is int else None,
            'stage': self.stage if type(self.stage) is str and self.stage in _FACT_STAGES else ''}


# LLM: 仅复用实际宿主 REST factory；缺失时拒绝，不读 desktop.yaml，不初始化 WS 或跨 owner 凭据。
# 函数用途: 取得原认证适配器及 tenant token，秘密仅保留于本次调用栈。
def _provider(agent):
    try:
        adapter = agent.delivery_service.registry.adapter_for('feishu')
        token = adapter._get_tenant_access_token() if adapter is not None else None
        if not token:
            raise FeishuProviderFactError('FEISHU_PROVIDER_AUTH_UNAVAILABLE', stage='auth')
        return adapter, token
    except (AttributeError, TypeError) as exc:
        raise FeishuProviderFactError('FEISHU_PROVIDER_UNAVAILABLE', stage='registry') from exc


# LLM: 原认证HTTP无重定向；stage只给错误定位，不发往平台；异常仅投固定原因/整数码，不携响应正文或URL。
# 函数用途: 读取原REST资源，区分权限、传输和坏响应；所有失败仍关闭授权，不自动重试或申请权限。
def _read(token, path, *, timeout=5.0, stage='response_validation'):
    from .feishu import _feishu_msg_api

    try:
        response = _feishu_msg_api('GET', path, {}, token, timeout=timeout)
    except HTTPError as exc:
        raise FeishuProviderFactError('FEISHU_PROVIDER_HTTP_REJECTED', exc.code, stage=stage) from exc
    except OSError as exc:
        raise FeishuProviderFactError('FEISHU_PROVIDER_TRANSPORT_UNAVAILABLE', stage=stage) from exc
    except ValueError as exc:
        raise FeishuProviderFactError('FEISHU_PROVIDER_RESPONSE_INVALID', stage=stage) from exc
    if not isinstance(response, dict) or type(response.get('code')) is not int:
        raise FeishuProviderFactError('FEISHU_PROVIDER_RESPONSE_INVALID', stage=stage)
    code = response['code']
    if code != 0:
        reason = 'FEISHU_PROVIDER_SCOPE_REQUIRED' if code in {99991663, 99991668, 99991672, 99991679, 230001} else 'FEISHU_PROVIDER_READ_REJECTED'
        raise FeishuProviderFactError(reason, code, stage=stage)
    data = response.get('data')
    if not isinstance(data, dict):
        raise FeishuProviderFactError('FEISHU_PROVIDER_RESPONSE_INVALID', stage=stage)
    return data


# LLM: 群与消息均是准确不透明 ID，编码后只访问单个资源；引用正文和 sender 不能变成 actor。
# 函数用途: 读取并核验一条当前群原消息，拒绝缺失、删除、跨群或身份不明确的结果。
def _message(token, chat_id, message_id):
    data = _read(token, '/im/v1/messages/' + quote(message_id, safe=''), stage='message')
    items = data.get('items')
    if not isinstance(items, list) or len(items) != 1 or not isinstance(items[0], dict):
        raise FeishuProviderFactError('FEISHU_SOURCE_MESSAGE_UNVERIFIED', stage='message')
    row = items[0]
    if row.get('message_id') != message_id or row.get('chat_id') != chat_id or row.get('deleted') is True:
        raise FeishuProviderFactError('FEISHU_SOURCE_MESSAGE_SCOPE_MISMATCH', stage='message')
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
        raise FeishuProviderFactError('FEISHU_PROVIDER_AUTH_UNAVAILABLE', stage='auth')
    return _group_authority_snapshot(adapter, token, chat_id)


# LLM: 只使用同一adapter及原群成员scope；此投影没有owner/bot证明，不能当群授权快照或代删授权。
# 函数用途: 给当前群缺名展示读取完整成员已有姓名，不调用通讯录、不缓存权限或猜身份。
def adapter_group_member_snapshot(adapter, chat_id):
    token = adapter._get_tenant_access_token()
    if not token:
        raise FeishuProviderFactError('FEISHU_PROVIDER_AUTH_UNAVAILABLE', stage='auth')
    members, names = _complete_group_members(token, chat_id, budget_seconds=2.0)
    return {'provider': 'feishu', 'chat_id': chat_id, 'members_complete': True,
        'member_ids': sorted(members), 'member_display_names': names}


# LLM: 只有全部官方open_id分页成功才返回成员及可选姓名；姓名独立投影，不影响权限、不额外查询或猜home写索引。
# 函数用途: 共用实际群角色和成员读取，保留同次认证响应已有姓名供宿主展示，不保存第二权限账。
def _group_authority_snapshot(adapter, token, chat_id):
    path = '/im/v1/chats/' + quote(chat_id, safe='')
    info = _read(token, path + '?user_id_type=open_id', stage='chat_info')
    own = _read(token, path + '/members/is_in_chat', stage='bot_membership')
    if own.get('is_in_chat') is not True or info.get('chat_mode') not in {'group', 'topic'}:
        raise FeishuProviderFactError('FEISHU_BOT_GROUP_MEMBERSHIP_REQUIRED', stage='bot_membership')
    if (info.get('owner_id_type') != 'open_id' or not isinstance(info.get('owner_id'), str)
            or not info['owner_id'] or info['owner_id'] != info['owner_id'].strip()):
        raise FeishuProviderFactError('FEISHU_GROUP_OWNER_UNVERIFIED', stage='chat_info')
    if not isinstance(info.get('tenant_key'), str) or not info['tenant_key'] or info['tenant_key'] != info['tenant_key'].strip():
        raise FeishuProviderFactError('FEISHU_GROUP_TENANT_UNVERIFIED', stage='chat_info')
    members, names = _complete_group_members(token, chat_id)
    return {'provider': 'feishu', 'channel': 'feishu', 'app_id': adapter.app_id, 'chat_id': chat_id,
        'tenant_key': info['tenant_key'], 'owner_id': info['owner_id'], 'owner_id_type': 'open_id',
        'member_ids': sorted(members), 'member_display_names': names, 'members_complete': True,
        'bot_is_member': True, 'observed_at': time.time()}


# LLM: 授权/展示/离群证明共用完整性解析；read_page只能是宿主同群认证reader，不能取模型入口；受限/重复/总数变动均拒绝。
# 函数用途: 在原页数/时间预算内读取完整open_id成员；可选总数有值须跨页稳定且最终一致，不发布部分姓名或负面离群证明。
def _complete_group_members(token, chat_id, *, budget_seconds=5.0, read_page=None):
    path = '/im/v1/chats/' + quote(chat_id, safe='')
    members, names, seen, cursor, deadline = set(), {}, set(), '', time.monotonic() + budget_seconds
    total, reader = None, _read if read_page is None else read_page
    for _page in range(10):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        params = {'member_id_type': 'open_id', 'page_size': 100}
        if cursor:
            params['page_token'] = cursor
        data = reader(token, path + '/members?' + urlencode(params), timeout=remaining, stage='members')
        if not isinstance(data, dict):
            break
        limited, count = data.get('trigger_security_conf_limit'), data.get('member_total')
        if (limited is not None and (type(limited) is not bool or limited)
                or count is not None and (type(count) is not int or count < 0 or total is not None and count != total)):
            break
        if count is not None:
            total = count
        rows = data.get('items')
        if time.monotonic() >= deadline or not isinstance(rows, list) or any(not isinstance(row, dict) or row.get('member_id_type') != 'open_id'
                or not isinstance(row.get('member_id'), str) or not row['member_id'] or row['member_id'] != row['member_id'].strip() for row in rows):
            break
        ids = [row['member_id'] for row in rows]
        if len(set(ids)) != len(ids) or members.intersection(ids):
            break
        members.update(ids)
        names.update({row['member_id']: row['name'].strip() for row in rows
            if isinstance(row.get('name'), str) and row['name'].strip()})
        if data.get('has_more') is False:
            if total is None or total == len(members):
                return members, names
            break
        cursor = data.get('page_token')
        if not isinstance(cursor, str) or not cursor or cursor in seen:
            break
        seen.add(cursor)
    raise FeishuProviderFactError('FEISHU_GROUP_MEMBERS_INCOMPLETE', stage='members')


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
