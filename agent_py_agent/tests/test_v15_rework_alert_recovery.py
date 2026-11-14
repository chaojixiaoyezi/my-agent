"""R2：真实投递账、事件锚点与新实例恢复；只控制提供方边界。"""
from __future__ import annotations

import hashlib
import json
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.conversation.background_delivery import BackgroundDeliveryDependencies
from agent_py_agent.agent.conversation.channels import DeliveryContext, ReplyEnvelope
from agent_py_agent.agent.delivery.registry import (
    ChannelAdapterRegistry,
    ChannelCapabilities,
    current_component_delivery,
)
from agent_py_agent.agent.delivery.service import DeliveryService
from agent_py_agent.agent.scheduler import alert_delivery
from agent_py_agent.agent.scheduler import alert_topic_anchors as anchors
from agent_py_agent.agent.scheduler.alert_topic_anchor_migration import migrate_legacy


class RecordingProvider:
    """执行原组件阶段状态机，不实现 lookup 或重放支持。"""

    def __init__(self, *, unknown=False, entered=None, release=None):
        self.calls = []
        self.unknown, self.entered, self.release = unknown, entered, release

    def _send(self, target, message, *, route):
        state = current_component_delivery()
        fact = state.ensure('text:0', message.metadata['delivery_idempotency_key'],
            hashlib.sha256(message.content.encode()).hexdigest())
        dispatched = state.before_send(fact, self)
        if dispatched is None:
            return fact
        self.calls.append((route, target, message))
        if self.entered is not None:
            self.entered.set()
            assert self.release.wait(5)
        response = {} if self.unknown else {'code': 0, 'data': {'message_id': f'om_confirmed_{len(self.calls)}'}}
        return state.finish(dispatched, response)

    def send_message(self, target, message):
        return self._send(target, message, route='root')

    def finalize_response(self, target, handle, message):
        return self._send(target, message, route='reply')


def _service(home, provider=None):
    registry = ChannelAdapterRegistry(delivery_state_root=home / 'data/channel-delivery')
    registry.register_adapter('feishu', provider, capabilities=ChannelCapabilities(
        text=True, proactive=True, reply=True, component_receipts=True, reply_policy=True))
    return DeliveryService(registry)


def _topic(home, service, *, event='evt-target', owner='owner-a', reconcile_only=False):
    agent = SimpleNamespace(home_paths=SimpleNamespace(owner_home_dir=home, owner_id=owner, owner_kind='group'),
        config=SimpleNamespace(im_topic_aggregation_enabled=True))
    request = SimpleNamespace(wake_signal={'source': 'fixture-source', 'event_id': event}, task_id='task-original')
    deps = BackgroundDeliveryDependencies(agent, None, service, lambda request: 'done')
    wrapped = alert_delivery._topic_delivery(deps, request, reconcile_only=reconcile_only)
    context = DeliveryContext('feishu', 'oc_fixture', thread_id='canonical', task_id=request.task_id,
        conversation_id='oc_fixture', idempotency_key='dispatch-original')
    return wrapped.channels, context


def _key(event='evt-target', *, owner='owner-a', target='oc_fixture'):
    return anchors.make_alert_key('fixture-source', owner, target, event)


def _current(home, key=None):
    from agent_py_agent.agent.common.json_io import locked_json_path

    key = key or _key()
    root, lock = anchors._prepare(home, key)
    with locked_json_path(lock):
        layout, data = anchors._current(root, key)
        return layout, data


def _layout(home, mode, provider):
    if mode == 'secondary':
        topic, context = _topic(home, _service(home, provider), event='evt-earlier')
        assert topic.deliver(context, ReplyEnvelope('earlier root')).delivery_status == 'sent'
        provider.calls.clear()
    elif mode == 'migrated':
        path = home / 'data/alert_topic_anchors.json'
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps({'schema_version': 'alert_topic_anchors.v1',
            'anchors': {_key('evt-earlier'): {'state': 'sent', 'root_message_id': 'om_earlier',
                'thread_id': '', 'updated_at': 'legacy'}},
            'root_delivery_keys': {_key('evt-earlier'): 'earlier-dispatch'}}))
        assert migrate_legacy(home)['complete'] is True
        assert anchors._read_ready(home)['primary_key'] == ''


def test_runtime_imports_exact_checkout():
    checkout = Path(__file__).resolve().parents[2]
    assert Path(alert_delivery.__file__).resolve() == checkout / 'agent_py_agent/agent/scheduler/alert_delivery.py'
    assert Path(anchors.__file__).resolve() == checkout / 'agent_py_agent/agent/scheduler/alert_topic_anchors.py'


@pytest.mark.parametrize('layout', ['primary', 'secondary', 'migrated'])
def test_proven_unsent_root_reopens_in_all_layouts(tmp_path, layout):
    provider = RecordingProvider()
    _layout(tmp_path, layout, provider)
    topic, context = _topic(tmp_path, _service(tmp_path))
    envelope = ReplyEnvelope('original root payload')
    assert topic.deliver(context, envelope).delivery_status == 'unavailable'
    assert provider.calls == []
    ready_before = anchors._read_ready(tmp_path)
    reopened, _ = _topic(tmp_path, _service(tmp_path, provider))
    assert reopened.deliver(context, envelope).delivery_status == 'sent'
    assert len(provider.calls) == 1 and provider.calls[0][0] == 'root'
    assert reopened.deliver(context, envelope).delivery_status == 'sent'
    assert len(provider.calls) == 1
    assert anchors._read_ready(tmp_path) == ready_before
    assert _current(tmp_path)[1]['anchors'][_key()]['state'] == 'sent'


def _fail_one_sent_write(monkeypatch):
    write = anchors._write
    failures = []

    def write_once(root, data):
        if not failures and any(row['state'] == 'sent' for row in data['anchors'].values()):
            failures.append(True)
            raise OSError('simulated sent anchor write failure')
        return write(root, data)

    monkeypatch.setattr(anchors, '_write', write_once)
    return failures


def test_confirmed_root_receipt_repairs_failed_anchor_and_routes_distinct_update(tmp_path, monkeypatch):
    provider = RecordingProvider()
    service = _service(tmp_path, provider)
    topic, context = _topic(tmp_path, service)
    envelope = ReplyEnvelope('original root payload', evidence_refs=('artifact://original',))
    failures = _fail_one_sent_write(monkeypatch)
    with pytest.raises(OSError, match='sent anchor write failure'):
        topic.deliver(context, envelope)
    stable_id = 'host-delivery:alert-topic:' + _key()
    terminal = service._host_delivery_store.terminal_receipt(stable_id)
    assert failures == [True] and len(provider.calls) == 1
    assert terminal['disposition'] == 'sent'
    assert terminal['component_receipts'][0]['provider_message_id'] == 'om_confirmed_1'
    assert _current(tmp_path)[1]['anchors'][_key()]['state'] == 'pending'

    restarted_service = _service(tmp_path, provider)
    restarted, _ = _topic(tmp_path, restarted_service, reconcile_only=True)
    assert restarted.deliver(context, envelope).delivery_status == 'sent'
    assert len(provider.calls) == 1
    update = replace(context, idempotency_key='dispatch-update')
    assert restarted.deliver(update, ReplyEnvelope('distinct later update')).delivery_status == 'sent'
    assert len(provider.calls) == 2
    route, target, message = provider.calls[-1]
    assert route == 'reply' and target == context.target
    assert message.content == 'distinct later update'
    assert message.metadata['reply_to'] == 'om_confirmed_1'
    assert message.metadata['reply_policy'] == 'thread'
    assert message.metadata['delivery_idempotency_key'] == 'dispatch-update'
    assert restarted_service._host_delivery_store.terminal_receipt(stable_id) == terminal
    assert _current(tmp_path)[1]['anchors'][_key()]['root_message_id'] == 'om_confirmed_1'


@pytest.mark.parametrize('layout', ['primary', 'secondary', 'migrated'])
@pytest.mark.parametrize('error_type', [OSError, InterruptedError])
def test_presend_error_or_cancellation_releases_exact_record_without_fd_leak(tmp_path, monkeypatch, layout, error_type):
    provider = RecordingProvider()
    _layout(tmp_path, layout, provider)
    service = _service(tmp_path, provider)
    topic, context = _topic(tmp_path, service)
    root, lock = anchors._prepare(tmp_path, _key())
    from agent_py_agent.agent.common.json_io import locked_json_path

    with locked_json_path(lock):
        pass
    lock_file = lock.with_name(lock.name + '.lock')
    lock_identity = (lock_file.stat().st_dev, lock_file.stat().st_ino)
    ready = anchors._read_ready(root)
    fd_dir = '/dev/fd' if Path('/dev/fd').is_dir() else '/proc/self/fd'
    before = len(os.listdir(fd_dir))

    def fail_prepare(*args, **kwargs):
        raise error_type('simulated presend interruption')

    with monkeypatch.context() as patch:
        patch.setattr(service, '_prepare', fail_prepare)
        with pytest.raises(error_type, match='presend interruption'):
            topic.deliver(context, ReplyEnvelope('original root'))
    assert provider.calls == []
    assert _current(tmp_path)[1]['anchors'] == {}
    if layout != 'primary':
        assert not root.joinpath(*anchors._record_parts(_key())).exists()
    assert (lock_file.stat().st_dev, lock_file.stat().st_ino) == lock_identity
    assert anchors._read_ready(root) == ready
    reopened, _ = _topic(tmp_path, _service(tmp_path, provider))
    assert reopened.deliver(context, ReplyEnvelope('original root')).delivery_status == 'sent'
    assert len(provider.calls) == 1
    assert len(os.listdir(fd_dir)) - before == 0


def test_later_update_can_repair_original_root_without_replaying_its_payload(tmp_path, monkeypatch):
    provider = RecordingProvider()
    topic, context = _topic(tmp_path, _service(tmp_path, provider))
    _fail_one_sent_write(monkeypatch)
    original = ReplyEnvelope('immutable original payload')
    with pytest.raises(OSError, match='sent anchor write failure'):
        topic.deliver(context, original)
    reopened, _ = _topic(tmp_path, _service(tmp_path, provider))
    assert reopened.deliver(replace(context, idempotency_key='different-update'),
        ReplyEnvelope('a different update')).delivery_status == 'sent'
    assert [(call[0], call[2].content) for call in provider.calls] == [
        ('root', original.content), ('reply', 'a different update')]
    assert reopened.deliver(context, original).delivery_status == 'sent'
    assert len(provider.calls) == 2


@pytest.mark.parametrize('damage', ['owner', 'store', 'dispatch', 'target', 'channel', 'conversation',
    'reply_to', 'policy', 'payload', 'request', 'unknown', 'logical-only'])
def test_mismatched_or_unconfirmed_durable_proof_fails_closed(tmp_path, monkeypatch, damage):
    from agent_py_agent.agent.common.json_io import locked_json_path

    provider = RecordingProvider()
    service = _service(tmp_path, provider)
    topic, context = _topic(tmp_path, service)
    _fail_one_sent_write(monkeypatch)
    envelope = ReplyEnvelope('immutable original root')
    with pytest.raises(OSError, match='sent anchor write failure'):
        topic.deliver(context, envelope)
    stable_id = 'host-delivery:alert-topic:' + _key()
    store = service._host_delivery_store
    if damage in {'owner', 'store', 'dispatch'}:
        root, lock = anchors._prepare(tmp_path, _key())
        with locked_json_path(lock):
            _, data = anchors._current(root, _key())
            if damage == 'dispatch':
                data['root_delivery_keys'][_key()] = 'foreign-dispatch'
            else:
                field = 'owner_id' if damage == 'owner' else 'store_root'
                data['anchors'][_key()]['root_delivery_proof'][field] = 'different-scope'
            anchors._write(root, data)
    else:
        path = store._sent_path(stable_id)
        terminal = json.loads(path.read_text())
        fields = {'target': 'target', 'channel': 'channel', 'conversation': 'conversation_id',
            'reply_to': 'reply_to', 'policy': 'reply_policy', 'payload': 'payload_sha256', 'request': 'request_id'}
        if damage in fields:
            terminal[fields[damage]] = 'different-value'
        elif damage == 'unknown':
            terminal['disposition'] = 'unknown'
        else:
            terminal['component_receipts'][0]['provider_message_id'] = ''
            terminal['receipt_id'] = 'logical-only-not-provider-id'
        path.write_text(json.dumps(terminal))
    terminal_before = store._sent_path(stable_id).read_bytes()
    reopened, _ = _topic(tmp_path, _service(tmp_path, provider))
    assert reopened.deliver(context, envelope).delivery_status == 'unknown'
    assert len(provider.calls) == 1
    assert _current(tmp_path)[1]['anchors'][_key()]['state'] != 'sent'
    assert store._sent_path(stable_id).read_bytes() == terminal_before


@pytest.mark.parametrize('state', ['pending', 'sent'])
def test_different_payload_cannot_claim_original_dispatch_receipt(tmp_path, monkeypatch, state):
    provider = RecordingProvider()
    topic, context = _topic(tmp_path, _service(tmp_path, provider))
    if state == 'pending':
        _fail_one_sent_write(monkeypatch)
        with pytest.raises(OSError, match='sent anchor write failure'):
            topic.deliver(context, ReplyEnvelope('root original'))
    else:
        assert topic.deliver(context, ReplyEnvelope('root original')).delivery_status == 'sent'
    reopened, _ = _topic(tmp_path, _service(tmp_path, provider))
    assert reopened.deliver(context, ReplyEnvelope('not the original root')).delivery_status == 'unknown'
    assert len(provider.calls) == 1
    assert reopened.deliver(context, ReplyEnvelope('root original')).delivery_status == 'sent'
    assert len(provider.calls) == 1


def test_unknown_provider_outcome_retains_original_proof_without_root_replay(tmp_path):
    provider = RecordingProvider(unknown=True)
    topic, context = _topic(tmp_path, _service(tmp_path, provider))
    assert topic.deliver(context, ReplyEnvelope('root original')).delivery_status != 'sent'
    before = _current(tmp_path)[1]
    reopened, _ = _topic(tmp_path, _service(tmp_path, provider))
    assert reopened.deliver(context, ReplyEnvelope('root original')).delivery_status == 'unknown'
    after = _current(tmp_path)[1]
    assert len(provider.calls) == 1 and after['anchors'][_key()]['state'] == 'unknown'
    assert after['root_delivery_keys'] == before['root_delivery_keys']
    assert after['anchors'][_key()]['root_delivery_proof'] == before['anchors'][_key()]['root_delivery_proof']


def test_same_event_has_one_real_sender_while_different_event_can_progress(tmp_path):
    entered, release = threading.Event(), threading.Event()
    provider = RecordingProvider(entered=entered, release=release)
    service = _service(tmp_path, provider)
    topic, context = _topic(tmp_path, service)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(topic.deliver, context, ReplyEnvelope('original root'))
        assert entered.wait(3)
        try:
            restarted, _ = _topic(tmp_path, _service(tmp_path, provider))
            second = pool.submit(restarted.deliver, context, ReplyEnvelope('original root'))
            assert second.result(timeout=3).delivery_status == 'unknown'
            assert len(provider.calls) == 1
            # 独立事件使用独立 provider 边界，不因前一个事件的网络等待持有 Owner 锁。
            independent_provider = RecordingProvider()
            independent, independent_context = _topic(tmp_path, _service(tmp_path, independent_provider), event='independent')
            other = pool.submit(independent.deliver, independent_context, ReplyEnvelope('independent root'))
            assert other.result(timeout=3).delivery_status == 'sent'
            assert len(independent_provider.calls) == 1
        finally:
            release.set()
        assert first.result(timeout=3).delivery_status == 'sent'
    assert restarted.deliver(context, ReplyEnvelope('original root')).delivery_status == 'sent'
    assert len(provider.calls) == 1


def test_recovery_rechecks_dispatch_under_event_lock_before_publishing(tmp_path, monkeypatch):
    from agent_py_agent.agent.common.json_io import locked_json_path

    provider = RecordingProvider()
    topic, context = _topic(tmp_path, _service(tmp_path, provider))
    _fail_one_sent_write(monkeypatch)
    with pytest.raises(OSError, match='sent anchor write failure'):
        topic.deliver(context, ReplyEnvelope('original root'))
    service = _service(tmp_path, provider)
    reopened, _ = _topic(tmp_path, service)
    read = service._host_delivery_store.terminal_receipt
    entered, release = threading.Event(), threading.Event()

    def paused(stable_id):
        receipt = read(stable_id)
        entered.set()
        assert release.wait(5)
        return receipt

    monkeypatch.setattr(service._host_delivery_store, 'terminal_receipt', paused)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(reopened.deliver, context, ReplyEnvelope('original root'))
        assert entered.wait(3)
        try:
            root, lock = anchors._prepare(tmp_path, _key())
            with locked_json_path(lock):
                _, data = anchors._current(root, _key())
                data['root_delivery_keys'][_key()] = 'changed-dispatch'
                anchors._write(root, data)
        finally:
            release.set()
        with pytest.raises(ValueError, match='identity changed'):
            future.result(timeout=3)
    assert _current(tmp_path)[1]['anchors'][_key()]['state'] == 'pending'
    assert len(provider.calls) == 1


def test_invalid_attachment_retains_original_service_rejection(tmp_path):
    provider = RecordingProvider()
    service = _service(tmp_path, provider)
    topic, context = _topic(tmp_path, service)
    envelope = ReplyEnvelope('invalid attachment', attachments=({},))
    expected = service.deliver(context, envelope)
    observed = topic.deliver(context, envelope)
    assert observed.delivery_status == expected.delivery_status == 'rejected'
    assert observed.error_code == expected.error_code == 'ARTIFACT_VALIDATION_FAILED'
    assert provider.calls == []
    assert _current(tmp_path)[1]['anchors'] == {}


def test_frozen_notification_commits_from_confirmed_root_without_duplicate_send(tmp_path, monkeypatch):
    from agent_py_agent.agent.conversation.background_delivery import (
        background_owner_delivery_committed,
        cached_owner_delivery,
        frozen_owner_delivery,
        record_background_response,
    )
    from agent_py_agent.tests.test_v15_w9_origin_sync import _setup

    deps, request, context, _origin = _setup(tmp_path)
    deps.agent.home_paths.owner_kind = 'group'
    deps.agent.config = SimpleNamespace(im_topic_aggregation_enabled=True)
    request.wake_signal.update(source='fixture-source', event_id='evt-target')
    context = replace(context, target='oc_main')
    original = 'the original frozen notification'

    def dependencies(provider=None):
        service = _service(tmp_path, provider)
        return alert_delivery._topic_delivery(replace(deps, channels=service), request, reconcile_only=True)

    first = record_background_response(dependencies(), request, context, original,
        deliver=True, delivery_reason='non_subagent_completion')
    assert first.delivery_status == 'unavailable' and first.outbox_frozen
    wake_id = request.wake_signal['wake_signal_id']
    frozen = cached_owner_delivery(deps.store.wakes.pending_one(wake_id))
    assert frozen['content'] == original and frozen['external_sent'] is False
    provider = RecordingProvider()
    failures = _fail_one_sent_write(monkeypatch)
    with pytest.raises(OSError, match='sent anchor write failure'):
        record_background_response(dependencies(provider), request, context, original,
            deliver=True, delivery_reason='non_subagent_completion', frozen_retry=frozen_owner_delivery(frozen))
    assert failures == [True] and len(provider.calls) == 1
    pending = cached_owner_delivery(deps.store.wakes.pending_one(wake_id))
    assert pending['content'] == original and pending['external_sent'] is False
    commit = record_background_response(dependencies(provider), request, context, original,
        deliver=True, delivery_reason='non_subagent_completion', frozen_retry=frozen_owner_delivery(pending))
    assert commit.delivery_status == 'sent' and commit.persisted
    assert background_owner_delivery_committed(request, target=context.target, ownership='external',
        commit=commit, canonical_record=True)
    assert len(provider.calls) == 1
    stored = deps.store.messages.recent(context.thread_id, limit=10)
    assert sum(row.content == original for row in stored) == 1
    # 完整 commit 才允许调用方确认原 wake；确认前保留原冻结证明，不能以缓存投影代替提交事实。
    deps.store.wakes.mark_handled(wake_id, now=__import__('time').time())
    assert deps.store.wakes.pending_one(wake_id) is None
