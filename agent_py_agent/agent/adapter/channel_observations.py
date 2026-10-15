# LLM: 结构化观察复用原 GatewayIngress store/worker，绝不构造人类 IncomingMessage、actor、占位或第二队列。
# 模块用途: 将 provider 观察保存为明确 observation 类型，并以宿主认证提交原 Gateway 观察入口。
from __future__ import annotations

import hashlib
import json
import logging
import time
from dataclasses import asdict
from urllib.request import Request

from ..common.authenticated_http import open_authenticated_request
from ..user_space.owner_resolver import OwnerIdentity, resolve_owner_home
from .ingress import GatewayIngressRecord, canonical_payload_digest


# LLM: 注册仍写原 manager/registry，没有第二 adapter 表；观察消费者只为显式声明入口的 adapter 接线。
# 函数用途: 原子保持原通道注册行为，并给观察接入同一个持久入站 worker。
def register_channel_adapter(manager, adapter):
    logger = logging.getLogger(__name__)
    name = adapter.adapter_name
    if name in manager._adapters:
        logger.warning('适配器 %s 已注册，将被替换', name)
    manager._adapters[name] = adapter
    if callable(getattr(adapter, 'on_observation', None)):
        adapter.on_observation(lambda event: route_channel_observation(manager, event))
    manager._delivery_registry.register_adapter(name, adapter, configured=True)
    logger.info('已注册通道适配器: %s', name)


# LLM: 身份只取真实 provider 回调；owner 由精确 chat 冻结，机器人来源不进入 user_id。
# 函数用途: 把一条观察登记到现有持久入站，成功后由原线程恢复和重试。
def route_channel_observation(manager, event):
    agent = manager._inbound_media_agent
    if agent is None or not manager._delivery_worker.store.durable:
        return False
    identity = OwnerIdentity.provider_group(event['channel'], event['chat_id'])
    owner = resolve_owner_home(agent.home_paths.root, identity)
    anchor = [event[key] for key in ('channel', 'app_id', 'tenant_key', 'chat_id', 'sender_id_type', 'sender_id', 'message_id')]
    ingress_id = hashlib.sha256(json.dumps(anchor, ensure_ascii=False).encode()).hexdigest()
    frozen = {**event, 'ingress_id': ingress_id}
    record = GatewayIngressRecord(ingress_id=ingress_id, kind='observation', channel=event['channel'],
        user_id='', conversation_id=event['chat_id'], provider_message_id=event['message_id'],
        canonical_payload_digest=canonical_payload_digest(frozen), content='',
        metadata={'observation': frozen, 'owner_id': owner.owner_id, 'host_request_owner.v1': asdict(identity)},
        timestamp=float(event.get('created_at') or 0), created_at=time.time(), updated_at=time.time())
    _record, _created, conflict = manager._delivery_worker.enqueue(record)
    if conflict or _record is None:
        return False
    if manager._lifecycle_started:
        manager._delivery_worker.start()
    return True


# LLM: 只使用原宿主 credential headers，来源 bot 不能成为 X-User-Id；503 保持原 worker 重试和相同正文。
# 函数用途: 提交已冻结观察，只有明确 accepted/ignored 才推进原入站到终态。
def submit_channel_observation(manager, payload):
    credentials = manager._reply_poll_client.credentials
    headers = {'Content-Type': 'application/json', 'X-Channel': str(payload['channel'])}
    headers.update(credentials.headers())
    request = Request(f'http://127.0.0.1:{manager.gateway_port}/channel-observations',
        data=json.dumps(payload, ensure_ascii=False).encode(), headers=headers)
    with open_authenticated_request(request, timeout=30) as response:
        result = json.loads(response.read().decode())
    if not isinstance(result, dict) or result.get('status') not in {'accepted', 'ignored'}:
        raise RuntimeError('CHANNEL_OBSERVATION_NOT_ACCEPTED')
    return result
