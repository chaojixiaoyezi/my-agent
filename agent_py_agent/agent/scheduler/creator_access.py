# LLM: 姓名只用于展示，缺名时原完整群成员投影补同一索引；代清理仍走原独立授权，姓名或缺资料不证明离开。
# 模块用途: 展示当前owner监控创建者并best-effort补已有姓名；代清理成员证明不受展示刷新影响。
from __future__ import annotations

import json
import logging
from urllib.request import Request

from ..common.authenticated_http import open_authenticated_request
from ..conversation.host_actor import HostActorError
from ..user_space.identity_store import lookup_provider_identity


# LLM: 名字不授权；每个creator仅读原索引，当前飞书群缺名时仅一次完整成员刷新，失败保持未知，不改变creator。
# 函数用途: 为同owner监控附显示名，必要时有界查原成员接口并补原索引，不查通讯录或跨群合并。
def creator_display_rows(agent, jobs):
    jobs = tuple(jobs)
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
            labels[key] = ' '.join(str(name).split())[:80]
    refreshed = _refresh_unknown_group_creators(agent, {key[1] for key, name in labels.items() if key[0] == 'feishu' and key[1] and not name})
    for job in jobs:
        creator = job.get('creator') or {}
        key = (creator.get('channel'), creator.get('actor_id'), creator.get('local') is True)
        name = labels[key]
        if key[0] == 'feishu' and not name:
            name = refreshed.get(key[1], '')
        label = f'{name or "姓名未知"}（{key[0]}:{key[1]}）' if all(key[:2]) else '本机创建者（local）' if creator == {'local': True} else '创建者身份待核验'
        result.append({**job, 'creator_display': label})
    return result


# LLM: 仅canonical当前群和缺名creator可触发，完整open_id结果才写原索引；网络在索引锁外，失败只记固定诊断。
# 函数用途: 每批展示至多查询一次群成员姓名，保持原身份关联及非阻塞写锁，不改变任务归属或授权。
def _refresh_unknown_group_creators(agent, subjects):
    from ..adapter.feishu_provider_facts import FeishuProviderFactError
    from ..user_space.group_authorization import group_chat_id
    from ..user_space.identity_store import remember_provider_display_names

    home = getattr(agent, 'home_paths', None)
    if not subjects or getattr(home, 'owner_kind', '') != 'group' or getattr(home, 'owner_provider', '') != 'feishu':
        return {}
    try:
        chat_id = group_chat_id(home)
        adapter = agent.delivery_service.registry.adapter_for('feishu')
        snapshot = adapter.group_member_snapshot(chat_id)
        if (not isinstance(snapshot, dict) or snapshot.get('provider') != 'feishu' or snapshot.get('chat_id') != chat_id
                or snapshot.get('members_complete') is not True or not isinstance(snapshot.get('member_ids'), list)
                or any(type(subject) is not str or not subject for subject in snapshot['member_ids'])
                or not isinstance(snapshot.get('member_display_names'), dict)):
            raise ValueError('invalid member display projection')
        members = set(snapshot['member_ids'])
        names = {subject: name for subject, name in snapshot['member_display_names'].items()
            if subject in subjects and subject in members and isinstance(name, str) and name.strip()}
        remember_provider_display_names(home, provider='feishu', names=names)
        return {subject: ' '.join(name.split())[:80] for subject, name in names.items()}
    except FeishuProviderFactError as exc:
        diagnostic = exc.safe_diagnostics()
        logging.getLogger(__name__).warning('scheduler_creator_display_refresh_failed reason_code=%s provider_code=%s stage=%s',
            diagnostic['reason_code'], diagnostic['provider_code'], diagnostic['stage'])
    except (OSError, RuntimeError, TypeError, ValueError, AttributeError) as exc:
        logging.getLogger(__name__).warning('scheduler_creator_display_refresh_failed error_type=%s', type(exc).__name__)
    return {}


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


# LLM: 原registry认证与2秒/十页/1MiB运输不变，完整性只交唯一provider解析器；未知或受限名单不能证明离群。
# 函数用途: 读取完整成员ID集合供原代删门使用；不新增权限、不接受模型成员声明，失败保持原固定安全码。
def current_group_members(agent, chat_id):
    from ..adapter.feishu_provider_facts import _complete_group_members

    try:
        adapter = agent.delivery_service.registry.adapter_for('feishu')
        token = adapter._get_tenant_access_token() if adapter is not None else None
        if not token:
            raise ValueError('unavailable')
        members, _names = _complete_group_members(token, chat_id, budget_seconds=2.0, read_page=_read_member_cleanup_page)
        return members
    except (OSError, RuntimeError, TypeError, ValueError, AttributeError):
        pass
    raise HostActorError('SCHEDULER_MEMBERSHIP_UNAVAILABLE')


# LLM: 此reader只供上方准确group/auth调用，path由唯一分页器构造；不新增URL入口，原1MiB读取上限及认证无跳转保留。
# 函数用途: 有界读取一页原代删成员响应，只拆成功data，字段完整性由共用解析器决定。
def _read_member_cleanup_page(token, path, *, timeout, stage):
    request = Request('https://open.feishu.cn/open-apis' + path, headers={'Authorization': 'Bearer ' + token})
    with open_authenticated_request(request, timeout=timeout) as response:
        data = response.read(1_048_577)
    if len(data) > 1_048_576:
        raise ValueError('member response limit exceeded')
    payload = json.loads(data.decode())
    if not isinstance(payload, dict) or type(payload.get('code')) is not int or payload['code'] != 0 or not isinstance(payload.get('data'), dict):
        raise ValueError('member response unavailable')
    return payload['data']


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
