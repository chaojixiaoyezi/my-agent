# LLM: 宿主认证观察只登记原 scheduler 事件；不创建人类 actor、/ask 请求或第二执行队列。真正 sender 身份仍经准确 provider Get 核验。
# 模块用途: 接受适配器原 Ingress 重投，将已授权群机器人消息接入原监控。
from __future__ import annotations

import math
from contextlib import ExitStack

from ..adapter.feishu_provider_facts import (
    FeishuProviderFactError,
    canonical_bot_observation,
    canonical_user_observation,
)
from ..user_space.owner_resolver import OwnerIdentity
from .request_worker import _resolve_request_agent_for_owner


# LLM: 所有字段来自认证 adapter 的冻结行；body 不接受 actor/owner/path/URL，机器人不能控制人工授权。
# 函数用途: 校验观察形状，明确拒绝缺身份、非机器人及未知内容类型。
def _validated_observation(body, channel):
    fields = {'channel', 'event_type', 'app_id', 'tenant_key', 'chat_id', 'message_id', 'sender_type',
        'sender_id', 'sender_id_type', 'created_at', 'content', 'message_type', 'parent_id', 'root_id', 'thread_id', 'ingress_id'}
    if (not isinstance(body, dict) or set(body) != fields or channel != 'feishu'
            or body.get('channel') != channel or body.get('event_type') != 'message'
            or body.get('sender_type') not in {'bot', 'app', 'user'} or body.get('sender_id_type') != 'open_id'):
        raise ValueError('invalid observation')
    for key in fields - {'created_at'}:
        if not isinstance(body[key], str):
            raise ValueError('invalid observation field')
    if any(not body[key] for key in ('app_id', 'tenant_key', 'chat_id', 'sender_id', 'message_id', 'ingress_id')):
        raise ValueError('incomplete observation')
    if type(body['created_at']) not in {int, float} or not math.isfinite(body['created_at']) or body['created_at'] < 0:
        raise ValueError('invalid observation time')
    return dict(body)


# LLM: owner 只由已认证 provider/chat 解析；创建时的能力授权不变为运行许可证，停止仍读原 job/generation。
# 函数用途: 将一条冻结观察登记给既有原群监控，没有订阅时不触发模型或查询历史。
def accept_channel_observation(base, event, *, probe_only=False, source_owner_cursor=None):
    from ..capability.runtime_config_reload import deployment_capability_config
    from ..scheduler.source_authorization import (
        SourceAuthorityDenied,
        authorize_personal_source,
        matching_source_views,
        personal_source_revalidator,
    )
    from ..user_space.owner_resolver import resolve_owner_home

    config = deployment_capability_config(base.home_paths)
    user = event['sender_type'] == 'user'
    key = 'feishu_user_subscriptions_enabled' if user else 'feishu_bot_subscriptions_enabled'
    if getattr(config, key, True) is not True:
        return {'status': 'ignored', 'reason_code': 'EVENT_SOURCE_DISABLED'}
    index = getattr(getattr(base, 'scheduler_repository', None), 'due_index', None)
    if index is None:
        return {'status': 'retryable', 'reason_code': 'EVENT_SOURCE_INDEX_RECOVERING'}
    group = OwnerIdentity.provider_group(event['channel'], event['chat_id'])
    group_home = resolve_owner_home(base.home_paths.root, group)
    owner = None
    if source_owner_cursor is None:
        try:
            group_home.scheduler_store_json.lstat()
        except FileNotFoundError:
            pass
        else:
            owner = group
    if owner == group and index is not None and index.source_owner_ready((group.provider, group.owner_kind, group.owner_id)):
        relevant = index.source_rule_refs(event, (group.provider, group.owner_kind, group.owner_id)) if user else index.source_chat_present(event)
        if not relevant:
            owner = None
    # bot/app 的实际来源 app_id 必须经同消息 Get，而不是拿 raw open_id 查询。
    canonical = None
    lookup = event
    if not user:
        if owner is None and index is not None and not index.source_chat_present(event):
            return {'status': 'ignored', 'reason_code': 'EVENT_SOURCE_NOT_SUBSCRIBED'}
        canonical = canonical_bot_observation(base, event)
        lookup = canonical
        group_key = (group.provider, group.owner_kind, group.owner_id)
        if owner == group and index.source_owner_ready(group_key) and not index.source_rule_refs(canonical, group_key):
            owner = None
    if owner is None:
        if index is None:
            raise RuntimeError('EVENT_SOURCE_INDEX_UNAVAILABLE')
        after = source_owner_cursor if source_owner_cursor and any(source_owner_cursor) else None
        candidate = index.source_owner_page(lookup, after=after) if index is not None else None
        if candidate is None:
            if not index.source_discovery_ready():
                return {'status': 'retryable', 'reason_code': 'EVENT_SOURCE_INDEX_RECOVERING'}
            return {'status': 'ignored', 'reason_code': 'EVENT_SOURCE_NOT_SUBSCRIBED'}
        owner = OwnerIdentity(*candidate)
    next_cursor = ['', '', ''] if owner == group and source_owner_cursor is None else [owner.provider, owner.owner_kind, owner.owner_id]
    with ExitStack() as leases:
        agent = _resolve_request_agent_for_owner(base, owner, lease_stack=leases)
        agent.delivery_service = base.delivery_service
        repository = agent.scheduler_repository
        owner_key = (owner.provider, owner.owner_kind, owner.owner_id)
        if index is None:
            raise RuntimeError('EVENT_SOURCE_INDEX_UNAVAILABLE')
        if not index.source_owner_ready(owner_key):
            # 一次原 owner 活动恢复；只有原规范账本完整验证后才签负面索引证明。
            with repository._mutation_scope() as (store, _admission):
                _jobs, errors = repository._valid_jobs(store)
                if errors:
                    raise RuntimeError('EVENT_SOURCE_STATE_INVALID')
                index.settle_source_owner(dict(zip(('provider', 'kind', 'id'), owner_key)), store)
        refs = index.source_rule_refs(lookup, owner_key)
        views = matching_source_views(repository, lookup, refs, probe_only=True)
        if not views:
            if index is not None:
                # 仅原 owner 写锁内的规范快照可以退休失效路由；不按索引猜无订阅。
                with repository._mutation_scope() as (store, _admission):
                    index.settle_source_owner(dict(zip(('provider', 'kind', 'id'), owner_key)), store)
            if probe_only:
                # 若另有 owner，供应商有界重投；刚清掉的失效首页不会永久挡住后页。
                more = index.source_owner_page(lookup, after=next_cursor if any(next_cursor) else None) if index else None
                if more or not index.source_discovery_ready():
                    return {'status': 'retryable', 'reason_code': 'EVENT_SOURCE_PROBE_CONTINUE'}
                return {'status': 'ignored', 'reason_code': 'EVENT_SOURCE_NOT_SUBSCRIBED'}
            if not index.source_discovery_ready():
                return {'status': 'retryable', 'reason_code': 'EVENT_SOURCE_INDEX_RECOVERING'}
            return {'status': 'partial', 'reason_code': 'EVENT_SOURCE_OWNER_CONTINUE', 'source_owner_cursor': next_cursor}
        if probe_only:
            return {'status': 'accepted', 'reason_code': 'EVENT_SOURCE_INTEREST_ONLY'}
        if canonical is None:
            canonical = canonical_user_observation(base, event)
        views = matching_source_views(repository, canonical, refs)
        allowed, checks, personal = set(), [], []
        for job, rule_id, generation, source in views:
            if 'subscriber_actor_id' in source:
                personal.append((job, rule_id, generation, source))
            elif owner != group:
                raise ValueError('EVENT_SOURCE_AUTHORITY_MISSING')
            allowed.add((job['job_id'], rule_id, generation))
        try:
            if personal:
                actors = {row[3]['subscriber_actor_id'] for row in personal}
                if len(actors) != 1:
                    raise RuntimeError('EVENT_SOURCE_MULTIPLE_ACTORS_UNSUPPORTED')
                # 同 owner / actor / 群只取一次原 OAuth token；各规则在末门仍独立核代次。
                authorize_personal_source(agent, repository, *personal[0])
                for row in personal:
                    check = personal_source_revalidator(agent, repository, *row)
                    check()
                    checks.append(check)
            for check in checks:
                check()
            def revalidate():
                for check in checks:
                    check()
            result = repository.accept_source_event(canonical, authorized_sources=allowed, revalidate=revalidate if checks else None)
        except SourceAuthorityDenied as denied:
            result = {'status': 'ignored', 'reason_code': denied.reason_code}
        if result['status'] == 'retryable':
            return result
        # 原 Ingress 保存精确后继 owner，每次最多一个宿主，不把第一页重复当完成。
        more = index.source_owner_page(lookup, after=next_cursor if any(next_cursor) else None) if index else None
        if not index.source_discovery_ready():
            return {'status': 'retryable', 'reason_code': 'EVENT_SOURCE_INDEX_RECOVERING'}
        return {**result, **({'status': 'partial', 'source_owner_cursor': next_cursor} if more else {})}


# LLM: 新入口必须有实际宿主凭据，即使旧 loopback 兼容开着；背压/权限不可核验返回失败以保留原 Ingress，不打印自由响应。
# 函数用途: 处理唯一观察 HTTP 入口，受理成功仅写回 typed receipt，不创建普通回复任务。
def handle_channel_observation(handler, server):
    middleware = getattr(handler, '_auth_middleware', None)
    if middleware is None or not middleware.has_gateway_credential(handler.headers):
        handler._send_json(403, {'status': 'rejected', 'reason_code': 'LOCAL_CREDENTIAL_REQUIRED'})
        return
    try:
        if server is None or server.agent is None or handler.headers.get('X-User-Id'):
            raise ValueError('invalid observation host')
        raw = handler._read_json()
        cursor = raw.pop('source_owner_cursor', None) if isinstance(raw, dict) else None
        if cursor is not None and (not isinstance(cursor, list) or len(cursor) != 3
                or any(not isinstance(value, str) or len(value) > 256 for value in cursor)
                or (any(cursor) and not all(cursor))):
            raise ValueError('invalid observation continuation')
        probe_only = raw.pop('probe_only', False) if isinstance(raw, dict) else False
        if type(probe_only) is not bool:
            raise ValueError('invalid observation probe')
        body = _validated_observation(raw, str(handler.headers.get('X-Channel') or ''))
        if probe_only and (body['sender_type'] != 'user' or cursor is not None):
            raise ValueError('invalid observation probe source')
        result = accept_channel_observation(server.agent, body, probe_only=probe_only, source_owner_cursor=cursor)
    except FeishuProviderFactError as exc:
        stable = exc.reason_code in {'FEISHU_BOT_LOOP_SUPPRESSED', 'FEISHU_OTHER_BOT_SOURCE_REQUIRED'}
        handler._send_json(200 if stable else 503, {'status': 'ignored' if stable else 'retryable', 'reason_code': exc.reason_code})
        return
    except (OSError, RuntimeError):
        handler._send_json(503, {'status': 'retryable', 'reason_code': 'EVENT_SOURCE_HOST_UNAVAILABLE'})
        return
    except (TypeError, ValueError, KeyError, AttributeError):
        handler._send_json(400, {'status': 'rejected', 'reason_code': 'EVENT_SOURCE_OBSERVATION_INVALID'})
        return
    handler._send_json(503 if result['status'] == 'retryable' else 200, result)
