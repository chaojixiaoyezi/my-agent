# LLM: 宿主认证观察只登记原 scheduler 事件；不创建人类 actor、/ask 请求或第二执行队列。真正 sender 身份仍经准确 provider Get 核验。
# 模块用途: 接受适配器原 Ingress 重投，将已授权群机器人消息接入原监控。
from __future__ import annotations

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
    if type(body['created_at']) not in {int, float} or body['created_at'] < 0:
        raise ValueError('invalid observation time')
    return dict(body)


# LLM: owner 只由已认证 provider/chat 解析；创建时的能力授权不变为运行许可证，停止仍读原 job/generation。
# 函数用途: 将一条冻结观察登记给既有原群监控，没有订阅时不触发模型或查询历史。
def accept_channel_observation(base, event, *, probe_only=False):
    from ..capability.runtime_config_reload import deployment_capability_config
    from ..scheduler.event_sources import event_rule_views
    from ..user_space.owner_resolver import resolve_owner_home

    config = deployment_capability_config(base.home_paths)
    user = event['sender_type'] == 'user'
    key = 'feishu_user_subscriptions_enabled' if user else 'feishu_bot_subscriptions_enabled'
    if getattr(config, key, True) is not True:
        return {'status': 'ignored', 'reason_code': 'EVENT_SOURCE_DISABLED'}
    owner = OwnerIdentity.provider_group(event['channel'], event['chat_id'])
    if probe_only:
        # 缺少规范规则库时不出生重宿主；仅明确缺失可作无订阅，权限/类型错误继续失败。
        source_home = resolve_owner_home(base.home_paths.root, owner)
        try:
            (source_home.scheduler_dir / 'store.json').lstat()
        except FileNotFoundError:
            return {'status': 'ignored', 'reason_code': 'EVENT_SOURCE_NOT_SUBSCRIBED'}
    with ExitStack() as leases:
        agent = _resolve_request_agent_for_owner(base, owner, lease_stack=leases)
        agent.delivery_service = base.delivery_service
        repository = agent.scheduler_repository
        jobs, errors = repository.list_jobs()
        if errors:
            return {'status': 'retryable', 'reason_code': 'EVENT_SOURCE_STATE_INVALID'}
        interested = False
        for job in jobs:
            if job.get('status') != 'active':
                continue
            for view in event_rule_views(job):
                source = view['schedule']['source']
                state = (job.get('event_source_state') or {}).get(str(view.get('rule_id') or '')) or {}
                if not view.get('enabled', True) or not state.get('active'):
                    continue
                if user and not (source['sender_type'] == 'user' and source['sender_id_type'] == 'open_id'
                        and source['app_id'] == event['app_id'] and source['chat_id'] == event['chat_id']
                        and event['sender_id'] in source['sender_ids']):
                    continue
                interested = True
        if not interested:
            return {'status': 'ignored', 'reason_code': 'EVENT_SOURCE_NOT_SUBSCRIBED'}
        if probe_only:
            return {'status': 'accepted', 'reason_code': 'EVENT_SOURCE_INTEREST_ONLY'}
        canonical = canonical_user_observation(agent, event) if user else canonical_bot_observation(agent, event)
        return repository.accept_source_event(canonical)


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
        probe_only = raw.pop('probe_only', False) if isinstance(raw, dict) else False
        if type(probe_only) is not bool:
            raise ValueError('invalid observation probe')
        body = _validated_observation(raw, str(handler.headers.get('X-Channel') or ''))
        if probe_only and body['sender_type'] != 'user':
            raise ValueError('invalid observation probe source')
        result = accept_channel_observation(server.agent, body, probe_only=probe_only)
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
