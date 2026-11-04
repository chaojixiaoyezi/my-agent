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


# LLM: 只在pytest临时目录读取产品实际锚点，不以reserve创建新的测试前提。
# 函数用途: 取得当前事件的持久状态。
def _stored_anchor(home):
    path = home / 'data/alert_topic_anchors.json'
    return json.loads(path.read_text())['anchors'].get(_key()) if path.exists() else None


# LLM: 使用正式record_alert_delivery入口；本例没有scheduler运行，必须由调用者继续原单路投递。
# 函数用途: 验证普通wake不被群thread偏好提升成告警根。
@pytest.mark.parametrize('configured', [False, True])
def test_non_scheduler_wake_does_not_enter_alert_topic(tmp_path, configured):
    channels = Channels()
    delivery = _delivery(tmp_path, channels)
    delivery.agent.config.im_topic_aggregation_enabled = configured
    request = SimpleNamespace(wake_signal={'wake_signal_id': 'ordinary-wake'}, task_id='background-task')
    context = DeliveryContext('feishu', 'oc_fixture', thread_id='canonical', task_id=request.task_id,
        mode='reply', reply_to='om_original', reply_policy='thread', provider_thread_id='omt_original')

    # LLM: 回调保留原包装参数，使错误分支真的走到通道并留下可核验的错误根。
    # 函数用途: 执行被测入口选择的投递视图。
    def record_one(dependencies, request, context, content):
        return dependencies.channels.deliver(context, ReplyEnvelope(content))

    result = alert_delivery.record_alert_delivery(delivery, request, context, 'ordinary reply',
        record_one=record_one, options={})
    assert result is None
    assert channels.calls == [] and _stored_anchor(tmp_path) is None
    receipt = delivery.channels.deliver(context, ReplyEnvelope('ordinary reply'))
    assert receipt.delivery_status == 'sent' and channels.calls == [context]
    assert channels.calls[0].reply_to == 'om_original' and channels.calls[0].mode == 'reply'
    assert not (tmp_path / 'data/alert_topic_anchors.json').exists()


class NativeProvider:
    """只替换最末发送端；实际DeliveryService和组件阶段账运行原实现。"""

    # LLM: 不实现lookup，恢复资格只能来自真实的未发送证据。
    # 函数用途: 记录提供方调用并配置已进入发送后的结果。
    def __init__(self, *, outcome='sent'):
        self.calls = []
        self.outcome = outcome

    # LLM: 在提供方入口记录派发，成功和未知都沿正式组件状态机发表。
    # 函数用途: 返回合成提供方确认或制造派发后异常。
    def send_message(self, target, message):
        from agent_py_agent.agent.delivery.registry import current_component_delivery

        self.calls.append((target, message))
        state = current_component_delivery()
        fact = state.ensure('text', message.metadata['delivery_idempotency_key'], 'fixture-payload')
        dispatched = state.before_send(fact, self)
        if dispatched is None:
            return fact
        if self.outcome == 'exception':
            raise OSError('simulated provider outcome unavailable')
        response = {'code': 0, 'data': {'message_id': 'om_provider_root'}} if self.outcome == 'sent' else {}
        return state.finish(dispatched, response)


# LLM: 不替换产品发送器，注册表和能力判断均走原公开接口。
# 函数用途: 创建可以在重试前重新登记适配器的真实投递服务。
def _native_service(provider=None):
    from agent_py_agent.agent.delivery.registry import ChannelAdapterRegistry, ChannelCapabilities
    from agent_py_agent.agent.delivery.service import DeliveryService

    registry = ChannelAdapterRegistry()
    capabilities = ChannelCapabilities(text=True, proactive=True, reply=True, component_receipts=True, reply_policy=True)
    registry.register_adapter('feishu', provider, capabilities=capabilities)
    return DeliveryService(registry), capabilities


# LLM: 本例仅配置告警视图，provider lookup始终缺失，失败后的成功必须真实调用发送端。
# 函数用途: 绑定本例固定事件和目标。
def _native_topic(home, service):
    request = SimpleNamespace(wake_signal={'source': 'fixture-source', 'event_id': 'evt-1'}, task_id='task')
    wrapped = alert_delivery._topic_delivery(_delivery(home, service), request)
    context = DeliveryContext('feishu', 'oc_fixture', thread_id='canonical', task_id='task', reply_policy='thread')
    return wrapped, context


# LLM: 不可用来自真实注册表且提供方从未调用，不能变成永久unknown欠账。
# 函数用途: 验证无lookup适配器恢复后可以重新发送首根。
def test_native_unavailable_before_dispatch_can_retry_without_lookup(tmp_path):
    service, capabilities = _native_service()
    wrapped, context = _native_topic(tmp_path, service)
    receipt = wrapped.channels.deliver(context, ReplyEnvelope('first'))
    assert receipt.delivery_status == 'unavailable'
    assert _stored_anchor(tmp_path) is None
    provider = NativeProvider()
    service.registry.register_adapter('feishu', provider, capabilities=capabilities)
    retried = wrapped.channels.deliver(context, ReplyEnvelope('retry'))
    assert retried.delivery_status == 'sent' and len(provider.calls) == 1
    assert _stored_anchor(tmp_path)['root_message_id'] == 'om_provider_root'


# LLM: 仅在发送服务的前置准备边界注入一次IO错误，真实提供方和重试链不被替换。
# 函数用途: 验证provider调用前的异常不会锁死首发预留。
def test_native_presend_exception_can_retry_without_lookup(tmp_path, monkeypatch):
    provider = NativeProvider()
    service, _ = _native_service(provider)
    wrapped, context = _native_topic(tmp_path, service)
    prepare = service._prepare
    failures = [True]

    # LLM: 原prepare尚未执行、更未进入provider，第二次恢复后沿同一正式入口。
    # 函数用途: 模拟一次确定发生在派发前的准备失败。
    def prepare_once(context, envelope, *, progress=False):
        if failures:
            failures.pop()
            raise OSError('simulated presend preparation failure')
        return prepare(context, envelope, progress=progress)

    monkeypatch.setattr(service, '_prepare', prepare_once)
    with pytest.raises(OSError, match='simulated presend preparation failure'):
        wrapped.channels.deliver(context, ReplyEnvelope('first'))
    assert provider.calls == [] and _stored_anchor(tmp_path) is None
    assert wrapped.channels.deliver(context, ReplyEnvelope('retry')).delivery_status == 'sent'
    assert len(provider.calls) == 1 and _stored_anchor(tmp_path)['state'] == 'sent'


# LLM: 进入提供方后的失败不能用failed标签或空确认误判为未派发。
# 函数用途: 验证原生发送异常与未知组件仍拒绝第二个根消息。
@pytest.mark.parametrize('outcome', ['exception', 'unknown'])
def test_native_postdispatch_uncertainty_still_blocks_root_retry(tmp_path, outcome):
    provider = NativeProvider(outcome=outcome)
    service, _ = _native_service(provider)
    wrapped, context = _native_topic(tmp_path, service)
    first = wrapped.channels.deliver(context, ReplyEnvelope('first'))
    assert first.delivery_status != 'sent'
    assert _stored_anchor(tmp_path)['state'] == 'unknown'
    assert wrapped.channels.deliver(context, ReplyEnvelope('retry')).delivery_status == 'unknown'
    assert len(provider.calls) == 1


# LLM: 本次没有provider调用不等于旧组件从未派发；原生sender重放旧unknown时仍必须停止。
# 函数用途: 验证旧组件事实不能被首发清理路径抹掉。
def test_prior_dispatched_component_is_not_released_on_unavailable(tmp_path):
    from agent_py_agent.agent.delivery.registry import (
        ComponentDeliveryState,
        ComponentReceipt,
        component_delivery_scope,
    )

    service, capabilities = _native_service()
    wrapped, context = _native_topic(tmp_path, service)
    prior = ComponentReceipt('text', 'alert-topic:' + _key(), first_dispatch_at=1.0,
        phase='unknown', effect_outcome='unknown')
    with component_delivery_scope(ComponentDeliveryState((prior,))):
        assert wrapped.channels.deliver(context, ReplyEnvelope('first')).delivery_status == 'unavailable'
    assert _stored_anchor(tmp_path)['state'] == 'unknown'
    provider = NativeProvider()
    service.registry.register_adapter('feishu', provider, capabilities=capabilities)
    assert wrapped.channels.deliver(context, ReplyEnvelope('retry')).delivery_status == 'unknown'
    assert provider.calls == []


# LLM: 并发跟随者只能观察未知；首发确定未调用provider后可以释放自己预留，第三次仍最多发一个根。
# 函数用途: 验证预发送失败与并发unknown投影交错不会永久锁死或重复发送。
def test_presend_failure_with_waiting_retry_can_recover(tmp_path, monkeypatch):
    entered, release = threading.Event(), threading.Event()
    provider = NativeProvider()
    service, _ = _native_service(provider)
    wrapped, context = _native_topic(tmp_path, service)
    prepare = service._prepare
    first = [True]

    # LLM: Event把首发悬停在正式prepare前，不靠sleep也不把错误注入到provider后。
    # 函数用途: 等并发重试观察完pending后再报告明确的发送前失败。
    def prepare_once(context, envelope, *, progress=False):
        if first:
            first.pop()
            entered.set()
            assert release.wait(5)
            raise OSError('simulated presend failure')
        return prepare(context, envelope, progress=progress)

    monkeypatch.setattr(service, '_prepare', prepare_once)
    with ThreadPoolExecutor(max_workers=2) as pool:
        initial = pool.submit(wrapped.channels.deliver, context, ReplyEnvelope('first'))
        assert entered.wait(3)
        try:
            following = pool.submit(wrapped.channels.deliver, context, ReplyEnvelope('waiting'))
            assert following.result(timeout=3).delivery_status == 'unknown'
            assert provider.calls == []
        finally:
            release.set()
        with pytest.raises(OSError, match='simulated presend failure'):
            initial.result(timeout=3)
    assert _stored_anchor(tmp_path) is None
    assert wrapped.channels.deliver(context, ReplyEnvelope('retry')).delivery_status == 'sent'
    assert len(provider.calls) == 1


# LLM: 清理只能删除当前唯一首发者未发送的预留，不能覆盖已确认或其它派发身份。
# 函数用途: 验证清理操作的精确所有权与sent单调性。
@pytest.mark.parametrize('confirmed,dispatch_key', [(True, 'original'), (False, 'other')])
def test_unsent_cleanup_never_removes_confirmed_or_foreign_reservation(tmp_path, confirmed, dispatch_key):
    anchors._reserve_for_delivery(tmp_path, _key(), now='first', dispatch_key='original')
    if confirmed:
        anchors.mark_sent(tmp_path, _key(), root_message_id='om_confirmed', thread_id='omt_confirmed', now='sent')
    before = (tmp_path / 'data/alert_topic_anchors.json').read_bytes()
    alert_delivery._release_unsent_topic(tmp_path, _key(), dispatch_key)
    assert (tmp_path / 'data/alert_topic_anchors.json').read_bytes() == before


# LLM: 使用真实飞书send/render/组件链；只替换认证查询和最终HTTP请求，缺token时不会调用消息API。
# 函数用途: 验证适配器已进入但消息尚未派发的确定失败也允许恢复。
def test_feishu_missing_auth_before_message_dispatch_can_retry(tmp_path, monkeypatch):
    from agent_py_agent.agent.adapter import feishu

    provider = object.__new__(feishu.FeishuAdapter)
    monkeypatch.setattr(provider, '_get_tenant_access_token', lambda: None)
    requests = []

    # LLM: 只记录已进入的消息API边界，不发网络；返回提供方真实协议形状。
    # 函数用途: 确认重试实际经过一次消息请求并取得根ID。
    def send(method, path, body, token):
        requests.append((method, path, body))
        return {'code': 0, 'data': {'message_id': 'om_provider_root'}}

    monkeypatch.setattr(feishu, '_feishu_msg_api_with_retry', send)
    service, _ = _native_service(provider)
    wrapped, context = _native_topic(tmp_path, service)
    assert wrapped.channels.deliver(context, ReplyEnvelope('first')).delivery_status == 'failed'
    assert requests == [] and _stored_anchor(tmp_path) is None
    monkeypatch.setattr(provider, '_get_tenant_access_token', lambda: 'synthetic-test-auth')
    assert wrapped.channels.deliver(context, ReplyEnvelope('retry')).delivery_status == 'sent'
    assert len(requests) == 1 and _stored_anchor(tmp_path)['root_message_id'] == 'om_provider_root'


# LLM: 旧适配器没有组件阶段合同，空快照不能证明未发送；进入其发送方法后必须保守未知。
# 函数用途: 验证旧provider异常不会因无组件回执而获得重发许可。
def test_legacy_provider_without_component_contract_keeps_unknown(tmp_path):
    calls = []

    # LLM: 只模拟不提供组件事实的旧发送端，错误发生在它已经被调用之后。
    # 函数用途: 返回无法确认外部结果的异常。
    def send(target, message):
        calls.append((target, message))
        raise OSError('simulated opaque provider outcome')

    provider = SimpleNamespace(send_message=send)
    service, capabilities = _native_service(provider)
    service.registry.register_adapter('feishu', provider,
        capabilities=replace(capabilities, component_receipts=False))
    wrapped, context = _native_topic(tmp_path, service)
    first = wrapped.channels.deliver(context, ReplyEnvelope('first'))
    assert first.delivery_status == 'failed' and first.component_receipts == ()
    assert _stored_anchor(tmp_path)['state'] == 'unknown'
    assert wrapped.channels.deliver(context, ReplyEnvelope('retry')).delivery_status == 'unknown'
    assert len(calls) == 1


# LLM: 观察视图不得另建persist、改变原state或替换共享registry；组件阶段仍在原sink按顺序提交。
# 函数用途: 验证首发观察没有绕过原组件账或泄漏到下一次发送。
def test_dispatch_observation_preserves_original_component_state_and_sink(tmp_path):
    from agent_py_agent.agent.delivery.registry import (
        ComponentDeliveryState,
        component_delivery_scope,
        current_component_delivery,
    )

    provider = NativeProvider()
    service, _ = _native_service(provider)
    registry = service.registry
    wrapped, context = _native_topic(tmp_path, service)
    snapshots = []
    state = ComponentDeliveryState(persist=snapshots.append)
    with component_delivery_scope(state):
        receipt = wrapped.channels.deliver(context, ReplyEnvelope('first'))
        assert current_component_delivery() is state
    assert receipt.delivery_status == 'sent' and state.snapshot() == receipt.component_receipts
    assert [snapshot[0]['phase'] for snapshot in snapshots] == ['accepted', 'unknown', 'send_confirmed']
    assert service.registry is registry and registry.adapter_for('feishu') is provider
