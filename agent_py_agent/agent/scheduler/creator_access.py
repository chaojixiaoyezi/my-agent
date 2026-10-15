# LLM: 监控显示名只读宿主身份索引；代清理只读官方当前完整成员快照，不保存第二权限账、不从姓名或缺资料推断离开。
# 模块用途: 为当前owner监控提供创建者显示和有界飞书成员证明，未知结果不赋予代删权限。
from __future__ import annotations

import json
import logging
import time
from urllib.parse import quote, urlencode
from urllib.request import Request

from ..common.authenticated_http import open_authenticated_request
from ..conversation.host_actor import HostActorError
from ..user_space.identity_store import lookup_provider_identity


# LLM: 名字只用于展示，每个不同creator仅读一次原身份索引；缺失/损坏保留稳定ID，不读USER.md、不改变授权。
# 函数用途: 为一批监控附加可信创建者显示名，不发模型或网络请求。
def creator_display_rows(agent, jobs):
    labels = {}
    result = []
    for job in jobs:
        creator = job.get('creator') or {}
        key = (creator.get('channel'), creator.get('actor_id'), creator.get('local') is True)
        if key not in labels:
            name = ''
            if all(key[:2]):
                try:
                    record = lookup_provider_identity(agent.home_paths, provider=key[0], provider_subject_id=key[1])
                    name = record.display_name if record else ''
                except (OSError, TypeError, ValueError, AttributeError):
                    pass
            name = ' '.join(str(name).split())[:80]
            labels[key] = f'{name or "姓名未知"}（{key[0]}:{key[1]}）' if all(key[:2]) else '本机创建者（local）' if creator == {'local': True} else '创建者身份待核验'
        result.append({**job, 'creator_display': labels[key]})
    return result


# LLM: 仅当前group owner与实际actor原群一致时访问官方成员列表；完整无creator才证明离群，当前操作者也必须存在。
# 函数用途: 对代为止损动作核验双方的实时成员状态；权限、网络、分页不完整均明确拒绝，不能当离职。
def authorize_departed_creator(agent, repository, job, actor, operation):
    creator = job.get('creator') or {}
    if (operation not in {'pause', 'cancel', 'delete'} or repository.owner.get('kind') != 'group'
            or repository.owner.get('provider') != 'feishu' or actor is None or actor.channel != 'feishu'
            or actor.owner_id != repository.owner['id']
            or actor.conversation_id != repository.owner['id'].split('/')[-1]
            or creator.get('channel') != 'feishu' or not creator.get('actor_id')):
        raise HostActorError('CHANNEL_ACTOR_SCOPE_MISMATCH')
    members = current_group_members(agent, actor.conversation_id)
    if actor.actor_id not in members:
        raise HostActorError('SCHEDULER_CURRENT_MEMBER_REQUIRED')
    if creator['actor_id'] in members:
        raise HostActorError('SCHEDULER_CREATOR_STILL_MEMBER')
    _audit_cleanup_authorization(agent, repository, job, actor, operation)


# LLM: 取得宿主原Delivery注册表的Feishu认证，不接收模型给的token/成员声明；十页和2秒查询预算，无缓存授权。
# 函数用途: 读取完整成员ID集合；坏响应、超预算或未知资格抛固定安全码，名字不作为身份。
def current_group_members(agent, chat_id):
    try:
        adapter = agent.delivery_service.registry.adapter_for('feishu')
        token = adapter._get_tenant_access_token() if adapter is not None else None
        if not token:
            raise ValueError('unavailable')
        deadline, cursor, seen, members = time.monotonic() + 2, '', set(), set()
        for _page in range(10):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            params = {'member_id_type': 'open_id', 'page_size': 100}
            if cursor:
                params['page_token'] = cursor
            path = '/im/v1/chats/' + quote(chat_id, safe='') + '/members?' + urlencode(params)
            request = Request('https://open.feishu.cn/open-apis' + path, headers={'Authorization': 'Bearer ' + token})
            with open_authenticated_request(request, timeout=remaining) as response:
                payload = json.loads(response.read(1_048_577).decode())
            if time.monotonic() >= deadline or not isinstance(payload, dict) or payload.get('code') != 0:
                break
            data = payload.get('data')
            if not isinstance(data, dict) or not isinstance(data.get('items'), list):
                break
            items = data['items']
            if any(not isinstance(item, dict) or not isinstance(item.get('member_id'), str) or not item['member_id'] for item in items):
                break
            members.update(item['member_id'] for item in items)
            if data.get('has_more') is False:
                return members
            cursor = data.get('page_token')
            if not isinstance(cursor, str) or not cursor or cursor in seen:
                break
            seen.add(cursor)
    except (OSError, RuntimeError, TypeError, ValueError, AttributeError):
        pass
    raise HostActorError('SCHEDULER_MEMBERSHIP_UNAVAILABLE')


# LLM: 复用原owner审计logger，仅记录已核验代清理意图；不把意图写成完成，不含名字/任务正文/成员名单或秘密。
# 函数用途: 保留原创建者、实际操作者及准确任务/版本来源；审计故障只报安全类型，不扩大授权或伪报执行成功。
def _audit_cleanup_authorization(agent, repository, job, actor, operation):
    try:
        from ..agent_core.audit_dispatch import _logger_for
        from ..audit.records import LogParams
        logger = _logger_for(agent.config)
        if logger is not None:
            logger.log(LogParams(action='SCHEDULER_CLEANUP_AUTHORIZED', user_id=actor.actor_id,
                channel=actor.channel, target_type='scheduler_job', target_id=job['job_id'], status='pending',
                details={'operation': operation, 'owner': repository.owner, 'creator': job['creator'],
                    'operator_actor_id': actor.actor_id, 'expected_version': job['version'],
                    'request_id': actor.entry_request_id, 'source_thread_id': actor.source_thread_id,
                    'creator_membership': 'absent', 'operator_membership': 'present'}))
    except (OSError, RuntimeError, TypeError, ValueError, AttributeError) as exc:
        logging.getLogger(__name__).warning('scheduler_cleanup_audit_unavailable error_type=%s', type(exc).__name__)
