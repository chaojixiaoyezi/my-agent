"""告警锚点的持久状态、并发与先核对后继续；无真实 IM 请求。"""
from __future__ import annotations

import hashlib
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.conversation.background_delivery import BackgroundDeliveryDependencies
from agent_py_agent.agent.conversation.channels import (
    DeliveryContext,
    DeliveryReceipt,
    ReplyEnvelope,
)
from agent_py_agent.agent.delivery.service import ComponentDeliveryReceipt
from agent_py_agent.agent.scheduler import alert_delivery
from agent_py_agent.agent.scheduler import alert_topic_anchors as anchors


# LLM: 测试输入不从运行时或用户目录取身份。
# 函数用途: 返回一个固定且可手工复算的告警键。
def _key(owner='owner-a', chat='oc_fixture', event='evt-1'):
    return hashlib.sha256(f'fixture-source|{owner}|{chat}|{event}'.encode()).hexdigest()


# LLM: 以 JSON 原件核验公共合同，不以 mock 调用次数代替状态证据。
# 函数用途: 验证预留幂等与已确认状态单调保留。
def test_reserve_and_sent_are_idempotent_and_monotonic(tmp_path):
    assert anchors.make_alert_key('fixture-source', 'owner-a', 'oc_fixture', 'evt-1') == _key()
    expected = {'root_message_id': '', 'thread_id': '', 'state': 'pending', 'updated_at': 'first'}
    assert anchors.reserve(tmp_path, _key(), now='first') == expected
    assert anchors.reserve(tmp_path, _key(), now='later') == expected
    sent = anchors.mark_sent(tmp_path, _key(), root_message_id='om_root', thread_id='omt_root', now='sent')
    assert sent['root_message_id'] == 'om_root' and sent['state'] == 'sent'
    assert anchors.mark_unknown(tmp_path, _key(), now='late') == sent
    data = json.loads((tmp_path / 'data/alert_topic_anchors.json').read_text())
    assert data['anchors'][_key()] == sent


# LLM: 未确认预留必须保留未知而不是删除，让恢复路径不能误当成从未发送。
# 函数用途: 验证未知状态和不安全键的拒绝。
def test_unknown_persists_and_path_keys_are_rejected(tmp_path):
    assert isinstance(anchors.reserve(tmp_path, _key(), now='start'), dict)
    assert anchors.mark_unknown(tmp_path, _key(), now='unknown').get('state') == 'unknown'
    with pytest.raises(ValueError):
        anchors.reserve(tmp_path, '../external', now='bad')


class Channels:
    """只模拟传输边界，锚点落盘及发送决策运行正式实现。"""

    # LLM: 仅保存合成回执与交错控制器，不访问网络。
    # 函数用途: 初始化可核对发送次数的通道。
    def __init__(self, *, unknown=False, lookup=None, entered=None, release=None):
        self.calls = []
        self.unknown = unknown
        self.entered, self.release = entered, release
        self.registry = SimpleNamespace(adapter_for=lambda channel: lookup)

    # LLM: 模拟真正的 provider message ID 与逻辑 receipt_id 分离。
    # 函数用途: 返回提供方级确认或未知结果。
    def deliver(self, context, envelope):
        self.calls.append(context)
        if self.entered is not None:
            self.entered.set()
            assert self.release.wait(5)
        return ComponentDeliveryReceipt(channel=context.channel, target=context.target,
            content=envelope.content, thread_id=context.thread_id, task_id=context.task_id,
            delivery_status='unknown' if self.unknown else 'sent', receipt_id='logical-not-a-provider-id',
            component_receipts=() if self.unknown else ({'phase': 'send_confirmed',
                'effect_outcome': 'confirmed', 'provider_message_id': 'om_provider_root'},))


# LLM: 所有根和配置均来自 tmp_path，每次 wrapper 仅绑定一次请求。
# 函数用途: 构造准确 Owner、群范围和只读发送偏好的依赖。
def _delivery(tmp_path, channels):
    agent = SimpleNamespace(home_paths=SimpleNamespace(owner_home_dir=tmp_path, owner_id='owner-a', owner_kind='group'),
        config=SimpleNamespace(im_topic_aggregation_enabled=True))
    return BackgroundDeliveryDependencies(agent, None, channels, lambda request: 'done')


# LLM: 同一事件后续更新只允许回复已确认提供方根，绝不把逻辑回执当外部消息 ID。
# 函数用途: 验证首发和后续更新共用一个话题。
def test_first_root_then_follow_up_uses_provider_anchor(tmp_path):
    channels = Channels()
    request = SimpleNamespace(wake_signal={'source': 'fixture-source', 'event_id': 'evt-1'}, task_id='task')
    wrapped = alert_delivery._topic_delivery(_delivery(tmp_path, channels), request)
    assert wrapped is not None
    context = DeliveryContext('feishu', 'oc_fixture', thread_id='canonical', task_id='task')
    assert wrapped.channels.deliver(context, ReplyEnvelope('first')).delivery_status == 'sent'
    wrapped.channels.deliver(replace(context, idempotency_key='update'), ReplyEnvelope('updated'))
    assert len(channels.calls) == 2
    assert channels.calls[0].mode == 'proactive'
    assert channels.calls[1].mode == 'reply' and channels.calls[1].reply_to == 'om_provider_root'
    assert channels.calls[1].reply_policy == 'thread'
    assert channels.calls[1].thread_id == 'canonical'
    assert anchors.reserve(tmp_path, _key(), now='inspect')['root_message_id'] == 'om_provider_root'


# LLM: 未知且没有 lookup 能力时必须停，不依赖提供方是否支持幂等的假设。
# 函数用途: 验证重试没有第二条创建根的网络调用。
def test_unknown_without_lookup_never_resends_root(tmp_path):
    channels = Channels(unknown=True)
    request = SimpleNamespace(wake_signal={'source': 'fixture-source', 'event_id': 'evt-1'}, task_id='task')
    wrapped = alert_delivery._topic_delivery(_delivery(tmp_path, channels), request)
    assert wrapped is not None
    context = DeliveryContext('feishu', 'oc_fixture')
    assert wrapped.channels.deliver(context, ReplyEnvelope('first')).delivery_status == 'unknown'
    retry = wrapped.channels.deliver(context, ReplyEnvelope('retry'))
    assert retry.delivery_status == 'unknown' and len(channels.calls) == 1


# LLM: Event 强制第一个发送悬停在提供方边界，第二个线程不能把 pending 误当发送许可。
# 函数用途: 验证并发只创建一个根。
def test_concurrent_reservation_allows_at_most_one_root(tmp_path):
    entered, release = threading.Event(), threading.Event()
    channels = Channels(entered=entered, release=release)
    request = SimpleNamespace(wake_signal={'source': 'fixture-source', 'event_id': 'evt-1'}, task_id='task')
    wrapped = alert_delivery._topic_delivery(_delivery(tmp_path, channels), request)
    assert wrapped is not None
    context = DeliveryContext('feishu', 'oc_fixture')
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(wrapped.channels.deliver, context, ReplyEnvelope('first'))
        assert entered.wait(3)
        try:
            second = pool.submit(wrapped.channels.deliver, context, ReplyEnvelope('second'))
            assert second.result(timeout=3).delivery_status == 'unknown'
            assert len(channels.calls) == 1
        finally:
            release.set()
        assert first.result(timeout=3).delivery_status == 'sent'


# LLM: 查询只接受同一事件和同一目标的提供方确认；重投原首发仅补已送达事实。
# 函数用途: 验证未知恢复先 lookup，且恢复首发不再网络发送。
def test_reconciliation_recovers_root_without_duplicate_delivery(tmp_path):
    anchors.reserve(tmp_path, _key(), now='start')
    anchors.mark_unknown(tmp_path, _key(), now='unknown')
    class Lookup:
        # LLM: 只返回合成且完整绑定的已确认根。
        # 函数用途: 模拟提供方的精确事件查询能力。
        def lookup_alert_root(self, *, alert_key, target_chat_id):
            assert alert_key == _key() and target_chat_id == 'oc_fixture'
            return {'state': 'sent', 'alert_key': alert_key, 'target_chat_id': target_chat_id,
                'root_message_id': 'om_found', 'thread_id': 'omt_found'}
    channels = Channels(lookup=Lookup())
    request = SimpleNamespace(wake_signal={'source': 'fixture-source', 'event_id': 'evt-1'}, task_id='task')
    wrapped = alert_delivery._topic_delivery(_delivery(tmp_path, channels), request, reconcile_only=True)
    assert wrapped is not None
    receipt = wrapped.channels.deliver(DeliveryContext('feishu', 'oc_fixture'), ReplyEnvelope('retry'))
    assert receipt.delivery_status == 'sent' and channels.calls == []
    assert anchors.reserve(tmp_path, _key(), now='inspect')['root_message_id'] == 'om_found'
