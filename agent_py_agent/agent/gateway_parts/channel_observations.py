# LLM: 宿主认证观察只登记原 scheduler 事件；不创建人类 actor、/ask 请求或第二执行队列。真正 sender 身份仍经准确 provider Get 核验。
# 模块用途: 接受适配器原 Ingress 重投，将已授权群机器人消息接入原监控。
from __future__ import annotations

from ..adapter.feishu_provider_facts import FeishuProviderFactError, canonical_bot_observation
from ..user_space.owner_resolver import OwnerIdentity
from .request_worker import _resolve_request_agent_for_owner


# LLM: 所有字段来自认证 adapter 的冻结行；body 不接受 actor/owner/path/URL，机器人不能控制人工授权。
# 函数用途: 校验观察形状，明确拒绝缺身份、非机器人及未知内容类型。
def _validated_observation(body, channel):
    fields = {'channel', 'event_type', 'app_id', 'tenant_key', 'chat_id', 'message_id', 'sender_type',
        'sender_id', 'sender_id_type', 'created_at', 'content', 'message_type', 'parent_id', 'root_id', 'thread_id', 'ingress_id'}
    if (not isinstance(body, dict) or set(body) != fields or channel != 'feishu'
            or body.get('channel') != channel or body.get('event_type') != 'message'
            or body.get('sender_type') not in {'bot', 'app'} or body.get('sender_id_type') != 'open_id'):
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
def accept_channel_observation(base, event):
    from ..capability.runtime_config_reload import deployment_capability_config

    config = deployment_capability_config(base.home_paths)
    if getattr(config, 'feishu_bot_subscriptions_enabled', True) is not True:
        return {'status': 'ignored', 'reason_code': 'EVENT_SOURCE_DISABLED'}
    owner = OwnerIdentity.provider_group(event['channel'], event['chat_id'])
    agent = _resolve_request_agent_for_owner(base, owner)
    agent.delivery_service = base.delivery_service
    repository = agent.scheduler_repository
    jobs, errors = repository.list_jobs()
    if errors:
        return {'status': 'retryable', 'reason_code': 'EVENT_SOURCE_STATE_INVALID'}
    if not any(job.get('event_source_state') for job in jobs):
        return {'status': 'ignored', 'reason_code': 'EVENT_SOURCE_NOT_SUBSCRIBED'}
    canonical = canonical_bot_observation(agent, event)
    return repository.accept_source_event(canonical)


# LLM: 新入口必须有实际宿主凭据，即使旧 loopback 兼容开着；背压/权限不可核验返回失败以保留原 Ingress，不打印自由响应。
# 函数用途: 处理唯一观察 HTTP 入口，受理成功仅写回 typed receipt，不创建普通回复任务。
def handle_channel_observation(handler, server):
    middleware = getattr(handler, '_auth_middleware', None)
    if middleware is None or not middleware.has_gateway_credential(dict(handler.headers)):
        handler._send_json(403, {'status': 'rejected', 'reason_code': 'LOCAL_CREDENTIAL_REQUIRED'})
        return
    try:
        if server is None or server.agent is None or handler.headers.get('X-User-Id'):
            raise ValueError('invalid observation host')
        body = _validated_observation(handler._read_json(), str(handler.headers.get('X-Channel') or ''))
        result = accept_channel_observation(server.agent, body)
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
