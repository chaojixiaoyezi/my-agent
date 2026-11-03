# LLM: 只允许原认证目标或带版本通知接管支持缺失来源兜底；不寻找最近会话、不复制源历史或扩大owner。
# 模块用途: 为显式通知任务解析独立执行线程，并保持原投递和认证门。
from __future__ import annotations

from types import SimpleNamespace

from .delivery_policy import (
    _repository_delivery_agent,
    available_delivery_targets,
    validate_delivery_targets,
)


# LLM: owner_fields由原repository提供；所有目标重走唯一validate_delivery_targets，字符串地址不构成授权。
# 函数用途: 为失去源线程的显式通知任务创建或复用同owner专用线程。
def resolve_scheduler_execution_thread(store, job, *, owner_fields):
    original_id = str(job.get('origin_thread_id') or job.get('source_thread_id') or job['thread_id'])
    original = store.threads.load(original_id)
    if original is not None:
        if ((original.owner_id and original.owner_id != owner_fields['owner_id'])
                or (original.owner_home and original.owner_home != owner_fields['owner_home'])):
            raise KeyError('定时来源owner不匹配')
        return original.thread_id, None
    repository = SimpleNamespace(owner=job['owner'], conversation_store=store,
        quota_enforcer=SimpleNamespace(owner_root=owner_fields['owner_home']))
    agent = _repository_delivery_agent(repository)
    agent = SimpleNamespace(home_paths=agent.home_paths, conversation_store=store)
    if (job['owner'] != repository.owner or agent.home_paths.owner_id != owner_fields['owner_id']
            or str(agent.home_paths.owner_home_dir) != owner_fields['owner_home']):
        raise KeyError('定时来源owner不匹配')
    policy = validate_delivery_targets(agent, job.get('delivery'))
    if not policy.get('targets') and job.get('notification_binding') is None:
        raise KeyError('定时来源会话不可用')
    rows = {row['binding_id']: row for row in available_delivery_targets(agent)}
    selected = [rows[spec['binding_id']] for spec in policy.get('targets', [])
        if spec.get('target_kind') == 'channel_binding' and rows[spec['binding_id']]['channel'] == 'feishu'
        and (rows[spec['binding_id']]['chat_id'] or rows[spec['binding_id']]['account_id'])]
    notification = job.get('notification_binding')
    if notification is not None:
        from .notification_binding import bound_notification_thread
        bound_notification_thread(agent, notification,
            follow_latest_channel_session=policy.get('follow_latest_channel_session', True))
    if not selected and notification is None:
        raise KeyError('定时来源会话不可用')
    thread = store.threads.get_or_create({'channel': 'scheduler',
        'channel_conversation_id': f"scheduler-job:{job['job_id']}",
        'canonical_user_id': owner_fields['owner_id'], 'owner_id': owner_fields['owner_id'],
        'owner_home': owner_fields['owner_home'], 'title': job['name']})
    if thread.owner_id != owner_fields['owner_id'] or thread.owner_home != owner_fields['owner_home']:
        raise KeyError('定时兜底线程owner不匹配')
    return thread.thread_id, {'reason': 'source_thread_unavailable', 'original_thread_id': original_id,
        'fallback_thread_id': thread.thread_id}


# LLM: 仅选择已验证的显式目标作为运输入口，后续多线发送仍逐目标重验；没有来源猜测或新地址。
# 函数用途: 解析兜底任务的初始投递线程，不把scheduler通道当飞书目标。
def fallback_delivery_thread(agent, run, *, channel=''):
    policy = validate_delivery_targets(agent, run.get('delivery'))
    rows = {row['binding_id']: row for row in available_delivery_targets(agent)}
    for spec in policy.get('targets', []):
        if spec.get('target_kind') != 'channel_binding':
            continue
        row = rows[spec['binding_id']]
        if channel in {'', 'scheduler', 'internal'} or row['channel'] == channel:
            return agent.conversation_store.threads.require(row['thread_id'])
    raise KeyError('定时显式投递目标不可用')
