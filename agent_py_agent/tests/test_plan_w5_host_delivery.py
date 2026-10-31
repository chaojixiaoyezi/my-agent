"""W5：原投递记录上的逐组件续收、时间窗和冻结目标。"""
from __future__ import annotations

import hashlib
from dataclasses import replace
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.adapter import delivery, feishu
from agent_py_agent.agent.adapter.delivery import GatewayReplyDeliveryStore, deliver_host_envelope
from agent_py_agent.agent.capability.channel_message_tool import SendMessageTool
from agent_py_agent.agent.delivery import (
    ChannelAdapterRegistry,
    ChannelAttachment,
    DeliveryContext,
    DeliveryService,
    ReplyEnvelope,
)
from agent_py_agent.agent.delivery.registry import ComponentDeliveryState, provider_replay_allowed
from agent_py_agent.agent.tooling.models import ToolOperationReconciliationContext


class LostReceipt(BaseException):
    pass


@pytest.fixture
def host_case(tmp_path, monkeypatch):
    case = SimpleNamespace(now=1000.0, uploads=[], sends=[], crash=True, store=GatewayReplyDeliveryStore(tmp_path / 'state'))
    monkeypatch.setattr(delivery.time, 'time', lambda: case.now)
    monkeypatch.setattr(delivery, '_dispatch_owner_live', lambda _: False)
    adapter = feishu.FeishuAdapter({'feishu_session_lock_enabled': False})
    adapter._get_tenant_access_token = lambda: 'synthetic-only'
    registry = ChannelAdapterRegistry()
    registry.register_adapter('feishu', adapter)
    case.registry, case.adapter, case.service = registry, adapter, DeliveryService(registry)
    attachments = []
    for index in range(2):
        path = tmp_path / f'part-{index}.bin'
        blob = f'original-{index}'.encode()
        path.write_bytes(blob)
        attachments.append(ChannelAttachment(f'artefact-{index}', str(path), path.name,
            sha256=hashlib.sha256(blob).hexdigest(), size_bytes=len(blob), owner_root=str(tmp_path)))
    case.envelope = ReplyEnvelope('正文' * 5000, tuple(attachments))
    case.context = DeliveryContext('feishu', 'oc_original', mode='reply', reply_to='om_original',
        request_id='request-original', idempotency_key='original-key', conversation_id='oc_original')

    def upload(blob, name, *_args):
        case.uploads.append((name, blob))
        case.now += 20  # 上传耗时不得进入消息去重窗口。
        return 'upload-' + name

    def api(method, path, body, _token):
        row = case.store.pending()[0]
        facts = row.dispatch['component_receipts']
        current = next(item for item in facts if item['message_key'] == body['uuid'])
        assert current['first_dispatch_at'] == case.now
        assert current['phase'] == 'unknown'
        if body['msg_type'] != 'text' and 'file_key' in body['content']:
            assert current['upload_ref']
        case.sends.append((path, body.copy()))
        if 'upload-part-1.bin' in body['content'] and case.crash:
            raise LostReceipt()
        return {'code': 0, 'data': {'message_id': f'om_out_{len(case.sends)}'}}

    monkeypatch.setattr(feishu, '_upload_feishu_bytes', upload)
    monkeypatch.setattr(feishu, '_feishu_msg_api_with_retry', api)
    return case


def _first_lost(case):
    with pytest.raises(LostReceipt):
        deliver_host_envelope(case.service, case.store, case.context, case.envelope, reply_policy='thread')
    row = case.store.pending()[0]
    assert len(row.dispatch['component_receipts']) == 4
    case.crash = False
    return row


def test_uploaded_attachments_and_confirmed_text_are_not_repeated_after_restart(host_case, monkeypatch):
    case = host_case
    old = _first_lost(case)
    facts = old.dispatch['component_receipts']
    last = facts[-1]
    assert last['upload_ref'] == 'upload-part-1.bin' and last['phase'] == 'unknown'
    assert last['first_dispatch_at'] == 1040.0
    assert [row['phase'] for row in facts[:3]] == ['send_confirmed'] * 3
    key, first = last['message_key'], last['first_dispatch_at']
    case.now += 125
    # 重试时 first_dispatch_at 保留原值，不是本次时钟。
    original = feishu._feishu_msg_api_with_retry

    def replay(method, path, body, token):
        assert body['uuid'] == key
        assert case.store.pending()[0].dispatch['component_receipts'][-1]['first_dispatch_at'] == first
        case.sends.append((path, body.copy()))
        return {'code': 0, 'data': {'message_id': 'om_reconciled'}}

    monkeypatch.setattr(feishu, '_feishu_msg_api_with_retry', replay)
    reopened = GatewayReplyDeliveryStore(case.store.root)
    receipt = deliver_host_envelope(case.service, reopened, case.context, case.envelope, reply_policy='thread')
    assert receipt.delivery_status == 'sent'
    assert len(case.uploads) == 2 and len(case.sends) == 5
    assert case.sends[-1][1]['uuid'] == key
    terminal = reopened.terminal_receipt(old.stable_id)
    assert terminal['component_receipts'][-1]['provider_message_id'] == 'om_reconciled'
    assert terminal['component_receipts'][-1]['first_dispatch_at'] == first
    assert original is not replay


@pytest.mark.parametrize('elapsed', [3600, 3601])
def test_expired_native_window_stays_unknown_without_upload_or_new_key(host_case, elapsed):
    case = host_case
    old = _first_lost(case)
    key = old.dispatch['component_receipts'][-1]['message_key']
    case.now += elapsed
    receipt = deliver_host_envelope(case.service, GatewayReplyDeliveryStore(case.store.root),
        case.context, case.envelope, reply_policy='thread')
    assert receipt.delivery_status == 'unknown'
    assert len(case.sends) == 4 and len(case.uploads) == 2
    terminal = case.store.terminal_receipt(old.stable_id)
    assert terminal['disposition'] == 'unknown'
    assert terminal['component_receipts'][-1]['message_key'] == key


def test_legacy_unknown_component_missing_first_dispatch_cannot_be_replayed(host_case):
    case = host_case
    old = _first_lost(case)
    facts = [dict(row) for row in old.dispatch['component_receipts']]
    facts[-1]['first_dispatch_at'] = None
    assert case.store.compare_and_swap(old, replace(old, dispatch={**old.dispatch, 'component_receipts': facts}))
    case.now += 125
    receipt = deliver_host_envelope(case.service, case.store, case.context, case.envelope, reply_policy='thread')
    assert receipt.delivery_status == 'unknown'
    assert len(case.sends) == 4 and len(case.uploads) == 2


def test_same_owner_sessions_keep_explicit_target_and_reject_changed_binding(tmp_path, monkeypatch):
    adapter = feishu.FeishuAdapter({'feishu_session_lock_enabled': False})
    adapter._get_tenant_access_token = lambda: 'synthetic-only'
    sent = []
    monkeypatch.setattr(feishu, '_feishu_msg_api_with_retry', lambda _m, path, body, _t:
        sent.append((path, body)) or {'code': 0, 'data': {'message_id': 'om_confirmed'}})
    registry = ChannelAdapterRegistry()
    registry.register_adapter('feishu', adapter)
    service, store = DeliveryService(registry), GatewayReplyDeliveryStore(tmp_path / 'shared-owner')
    contexts = [DeliveryContext('feishu', f'oc_session_{n}', mode='reply', reply_to=f'om_in_{n}',
        conversation_id=f'oc_session_{n}', idempotency_key=f'key-{n}') for n in (1, 2)]
    for context in contexts:
        assert deliver_host_envelope(service, store, context, ReplyEnvelope('正文')).delivery_status == 'sent'
    assert [path for path, _ in sent] == ['/im/v1/messages/om_in_1/reply', '/im/v1/messages/om_in_2/reply']
    wrong = replace(contexts[0], target='oc_session_2', reply_to='om_in_2')
    assert deliver_host_envelope(service, store, wrong, ReplyEnvelope('正文')).delivery_status == 'unknown'
    assert len(sent) == 2


@pytest.mark.parametrize('moment,allowed', [(1001, True), (4599.99, True), (4600, False), (999, False)])
def test_message_tool_and_normal_reply_use_identical_window(tmp_path, monkeypatch, moment, allowed):
    from agent_py_agent.agent.delivery import registry as registry_module

    monkeypatch.setattr(registry_module.time, 'time', lambda: moment)
    provider = feishu.FeishuAdapter({'feishu_session_lock_enabled': False})
    registry = ChannelAdapterRegistry()
    registry.register_adapter('feishu', provider)
    agent = SimpleNamespace(config=SimpleNamespace(my_agent_owner_id='ou_original'),
        home_paths=SimpleNamespace(owner_provider='feishu', owner_id='owner', owner_home_dir=tmp_path),
        delivery_service=DeliveryService(registry))
    tool = SendMessageTool(agent)
    fact = {'component_id': 'text:0', 'message_key': 'native-key', 'first_dispatch_at': 1000.0,
        'phase': 'unknown', 'effect_outcome': 'unknown'}
    context = ToolOperationReconciliationContext('owner', 'run', 'task', 'operation', 'send_message',
        'hash', 'business-key', 'business', {'component_receipts': [fact]})
    result = tool.reconcile_operation({}, context)
    assert (result.outcome == 'safe_to_retry') is allowed
    assert provider_replay_allowed(provider, fact) is allowed
    if not allowed:
        assert result.outcome == 'unknown'


def test_upload_confirmed_is_not_delivery_confirmed():
    from agent_py_agent.agent.delivery.registry import ComponentReceipt

    state = ComponentDeliveryState()
    receipt = state.save(ComponentReceipt('attachment:0', 'original-key', phase='upload_confirmed', upload_ref='upload-key'))
    assert not receipt and receipt.provider_message_id is None


@pytest.mark.parametrize('elapsed', [125, 3600])
def test_default_delivery_service_background_entry_recovers_original_durable_components(host_case, monkeypatch, tmp_path, elapsed):
    from agent_py_agent.agent.delivery import build_default_channel_registry

    case = host_case
    config = SimpleNamespace(my_agent_home=str(tmp_path / 'canonical-home'))

    def service():
        registry = build_default_channel_registry(config)
        registry.register_adapter('feishu', case.adapter)
        return DeliveryService(registry)

    case.service = service()
    case.store = case.service._host_delivery_store
    with pytest.raises(LostReceipt):
        case.service.deliver(case.context, case.envelope, reply_policy='thread')
    old = case.store.pending()[0]
    first = old.dispatch['component_receipts'][-1]['first_dispatch_at']
    key = old.dispatch['component_receipts'][-1]['message_key']
    assert first == 1040.0 and len(case.uploads) == 2 and len(case.sends) == 4
    case.now += elapsed
    case.crash = False

    def replay(_method, path, body, _token):
        assert body['uuid'] == key
        assert case.store.pending()[0].dispatch['component_receipts'][-1]['first_dispatch_at'] == first
        case.sends.append((path, body.copy()))
        return {'code': 0, 'data': {'message_id': 'om_default_reconciled'}}

    monkeypatch.setattr(feishu, '_feishu_msg_api_with_retry', replay)
    restarted = service()
    assert restarted._host_delivery_store is not case.store
    assert restarted._host_delivery_store.root == case.store.root
    receipt = restarted.deliver(case.context, case.envelope, reply_policy='thread')
    assert len(case.uploads) == 2
    assert len(case.sends) == (5 if elapsed < 3600 else 4)
    assert receipt.delivery_status == ('sent' if elapsed < 3600 else 'unknown')
    terminal = case.store.terminal_receipt(old.stable_id)
    assert terminal['component_receipts'][-1]['first_dispatch_at'] == first
    assert terminal['component_receipts'][-1]['message_key'] == key
