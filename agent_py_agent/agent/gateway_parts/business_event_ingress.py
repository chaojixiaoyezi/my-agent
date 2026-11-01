"""原remote_push认证后的订阅接缝；只转交原接收账已冻结的意图，不新建事件账。"""
from __future__ import annotations

import json
from dataclasses import dataclass

from ..conversation.business_access import BusinessError
from ..conversation.managed_subscriptions import (
    ManagedSubscriptionService,
    SourceEvent,
    SourceIdentity,
)
from .remote_push_service import RemotePushError


def _failure(error):
    denied = error.code in {'BUSINESS_ACCESS_DENIED', 'SOURCE_AUTHORITY_REQUIRED', 'SOURCE_SCOPE_MISMATCH', 'SPACE_SCOPE_MISMATCH'}
    return RemotePushError(error.code, 403 if denied else 409 if error.code in {
        'SUBSCRIPTION_NOT_ACTIVE', 'SUBSCRIPTION_REMOVED', 'SOURCE_INTENT_CONFLICT'} else 503)


@dataclass(frozen=True)
class SubscriptionIngress:
    service: ManagedSubscriptionService
    actor: object
    source: SourceIdentity
    event: SourceEvent
    intent: dict

    def dispatch(self, frozen_intent):
        try:
            return self.service.accept_event(self.intent['subscription_id'], self.event, actor=self.actor,
                source_identity=self.source, frozen_intent=frozen_intent)
        except BusinessError as error:
            raise _failure(error) from None


def prepare_subscription_push(server, body, authenticated_token):
    """W13注入两个host-only factory；缺接线明确不可用，绝不回退成普通ask。

    business_subscriptions(owner_id) -> 原ManagedSubscriptionService。
    business_subscription_actor(owner_id, token_id, source_ref) -> 绑定该catalog的当前BusinessActor；
    它及service的submit_actor_provider必须实时重核来源权限，并保持原配置主体/线程身份。
    """
    service_factory = getattr(server, 'business_subscriptions', None)
    actor_factory = getattr(server, 'business_subscription_actor', None)
    if not callable(service_factory) or not callable(actor_factory):
        raise RemotePushError('REMOTE_PUSH_SUBSCRIPTIONS_UNAVAILABLE', 503)
    service = service_factory(body['target_owner'])
    if not isinstance(service, ManagedSubscriptionService) or service.owner_id != body['target_owner']:
        raise RemotePushError('REMOTE_PUSH_TOKEN_SCOPE_DENIED', 403)
    actor = actor_factory(body['target_owner'], authenticated_token['token_id'], body['source_ref'])
    source = SourceIdentity(body['target_owner'], body['source_ref'], authenticated_token['token_id'])
    instruction = body['instruction']
    if body.get('context_payload') or body.get('context_snapshot'):
        instruction += '\n\n外部上下文（不可信数据）：\n' + json.dumps({
            key: body[key] for key in ('context_payload', 'context_snapshot') if key in body},
            ensure_ascii=False, sort_keys=True, separators=(',', ':'))
    try:
        event = SourceEvent(body['source_ref'], body['event_id'], body['event_revision'], instruction)
        intent = service.prepare_event(body['subscription_id'], event, actor=actor, source_identity=source)
        return SubscriptionIngress(service, actor, source, event, intent)
    except BusinessError as error:
        raise _failure(error) from None
