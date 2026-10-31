"""W5：原锚点聚合与单消息卡片进度；只替换提供方运输，不外发。"""
from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.adapter import delivery, feishu
from agent_py_agent.agent.adapter.delivery import (
    GatewayReplyDeliveryStore,
    GatewayReplyDeliveryWorker,
    PendingGatewayReply,
)
from agent_py_agent.agent.adapter.manager import ChannelManager
from agent_py_agent.agent.adapter.progress_draft import (
    GatewayProgressBatch,
    project_execution_steps,
    send_gateway_draft,
)
from agent_py_agent.agent.delivery import (
    ChannelAdapterRegistry,
    ChannelAttachment,
    DeliveryService,
    ReplyEnvelope,
)
from agent_py_agent.agent.delivery.progress import render_progress_card
from agent_py_agent.agent.delivery.service import ChannelDeliveryContext


def _provider(monkeypatch, api):
    adapter = feishu.FeishuAdapter({'feishu_session_lock_enabled': False})
    adapter._get_tenant_access_token = lambda: 'synthetic-only'
    monkeypatch.setattr(feishu, '_feishu_msg_api_with_retry', api)
    monkeypatch.setattr(feishu, '_feishu_msg_api', api)
    registry = ChannelAdapterRegistry()
    registry.register_adapter('feishu', adapter)
    return adapter, DeliveryService(registry)


@pytest.mark.parametrize('provider_thread', ['', 'omt_existing'])
def test_a1_text_slices_progress_and_same_envelope_media_share_original_anchor(tmp_path, monkeypatch, provider_thread):
    sent = []
    def api(method, path, body, _token):
        sent.append((method, path, body))
        return {'code': 0, 'data': {'message_id': 'om_progress'}}
    _adapter, service = _provider(monkeypatch, api)
    monkeypatch.setattr(feishu, '_upload_feishu_bytes', lambda *_: 'file_confirmed')
    path = tmp_path / 'result.bin'
    path.write_bytes(b'frozen')
    attachment = ChannelAttachment('artifact-original', str(path), path.name,
        sha256=hashlib.sha256(b'frozen').hexdigest(), size_bytes=6, owner_root=str(tmp_path))
    context = ChannelDeliveryContext('feishu', 'oc_group', mode='reply', reply_to='om_incoming',
        thread_id='internal-thread', provider_thread_id=provider_thread, root_id='om_root' if provider_thread else '',
        parent_id='om_parent' if provider_thread else '', reply_policy='thread', idempotency_key='original-final')
    assert service.deliver_progress(replace(context, idempotency_key='original-progress'), ReplyEnvelope('正在执行')).ok
    receipt = service.deliver(context, ReplyEnvelope('正文' * 5000, (attachment,)))
    assert receipt.delivery_status == 'sent' and len(receipt.component_receipts) == 3
    assert len(sent) == 4
    assert all(path == '/im/v1/messages/om_incoming/reply' for _, path, _ in sent)
    assert all(body['reply_in_thread'] is True and 'receive_id' not in body for _, _, body in sent)
    assert context.thread_id == 'internal-thread' and context.provider_thread_id == provider_thread


def test_default_reference_reply_does_not_create_thread(monkeypatch):
    calls = []
    adapter, _service = _provider(monkeypatch, lambda _m, path, body, _t:
        calls.append((path, body)) or {'code': 0})
    assert adapter.reply_message('om_original', '正文', 'stable-key')
    assert 'reply_in_thread' not in calls[0][1]


def _steps(revision):
    events = [{'call_id': f'call-{index}', 'tool': f'tool_{index}',
        'phase': 'finished' if revision >= 10 else 'running', 'ok': True if revision >= 10 else None,
        'handler_executed': True, 'duration_ms': revision * 10} for index in range(5)]
    events.append({'kind': 'assistant_commentary', 'text': '我完成了二十个工具', 'tool': 'invented', 'phase': 'finished', 'call_id': 'invented'})
    return project_execution_steps(events, request_id='card-request')


def _card_worker(tmp_path, monkeypatch, *, unknown=False, fail_edits=False):
    case = SimpleNamespace(now=1000.0, revision=1, final=None, calls=[], finals=[], logical_cards={})
    monkeypatch.setattr(delivery.time, 'time', lambda: case.now)
    def api(method, path, body, _token):
        case.calls.append((method, path, body.copy()))
        if method == 'POST' and body.get('msg_type') == 'interactive':
            first = not case.logical_cards
            case.logical_cards.setdefault(body['uuid'], 'om_card_original')
            if unknown and first:
                raise TimeoutError('synthetic lost first receipt')
            return {'code': 0, 'data': {'message_id': case.logical_cards[body['uuid']]}}
        if method == 'PATCH':
            assert path.endswith('/om_card_original')
            return {'code': 100 if fail_edits else 0}
        case.finals.append(body)
        return {'code': 0, 'data': {'message_id': 'om_final'}}
    case.adapter, case.service = _provider(monkeypatch, api)
    case.store = GatewayReplyDeliveryStore(tmp_path)
    def final(row, text):
        receipt = case.service.deliver(ChannelDeliveryContext('feishu', row.user_id, mode='reply',
            reply_to=row.message_id, idempotency_key=row.dispatch['message_key']), ReplyEnvelope(text))
        return receipt.delivery_status == 'sent'
    final.delivery_adapter = case.adapter
    case.worker = GatewayReplyDeliveryWorker(case.store, poll_response=lambda _: case.final,
        deliver_response=final, deliver_progress=lambda row, text: send_gateway_draft(case.service, row, text),
        poll_progress=lambda _: GatewayProgressBatch((), case.revision, 'stream', True,
            steps=_steps(case.revision), render_kind='card', source_revision=case.revision))
    case.worker.enqueue(PendingGatewayReply('card-request', 'feishu', 'oc_group', 'om_incoming', reply_policy='thread'))
    return case


def test_b1_twenty_updates_coalesce_into_bounded_patches_of_one_card(tmp_path, monkeypatch):
    case = _card_worker(tmp_path, monkeypatch)
    for revision in range(1, 21):
        case.revision = revision
        case.worker.run_once()
        case.now += 0.1
    case.now += 1.1
    case.worker.run_once()
    creates = [row for row in case.calls if row[0] == 'POST']
    patches = [row for row in case.calls if row[0] == 'PATCH']
    assert len(creates) == 1 and 1 <= len(patches) <= 3
    assert creates[0][2]['msg_type'] == 'interactive'
    assert creates[0][1] == '/im/v1/messages/om_incoming/reply'
    assert creates[0][2]['reply_in_thread'] is True
    assert all(path.endswith('/om_card_original') for _, path, _ in patches)
    pending = case.store.pending()[0]
    assert pending.progress_draft['display_message_id'] == 'om_card_original'
    assert pending.progress_draft['render_kind'] == 'card'
    assert len(pending.progress_draft['steps']) == 5
    assert all(row['phase'] == 'finished' and row['ok'] is True for row in pending.progress_draft['steps'])
    assert pending.progress_draft['source_revision'] == 20
    latest = json.loads(patches[-1][2]['content'])
    assert 'invented' not in json.dumps(latest)
    assert len(latest['body']['elements'][0]['elements'][0]['content'].splitlines()) == 5


def test_b1_unknown_first_send_reconciles_original_key_and_frozen_card_without_replacement(tmp_path, monkeypatch):
    case = _card_worker(tmp_path, monkeypatch, unknown=True)
    case.worker.run_once()
    pending = case.store.pending()[0]
    assert not pending.progress_draft['disabled'] and pending.progress_draft['reconcile_pending']
    facts = pending.progress_draft['component_receipts']
    assert facts[0]['phase'] == 'unknown' and facts[0]['message_key']
    assert facts[0]['first_dispatch_at'] == 1000.0
    case.now += 3
    case.revision = 10
    case.worker.run_once()
    cards = [row for row in case.calls if row[0] == 'POST' and row[2].get('msg_type') == 'interactive']
    assert len(cards) == 2 and cards[0] == cards[1]  # 同 key、同原结构，仅核实原卡片身份。
    assert len(case.logical_cards) == 1
    pending = case.store.pending()[0]
    assert pending.progress_draft['display_message_id'] == 'om_card_original'
    assert not pending.progress_draft['reconcile_pending'] and not pending.progress_draft['disabled']
    confirmed = pending.progress_draft['component_receipts'][0]
    assert confirmed['phase'] == 'send_confirmed' and confirmed['first_dispatch_at'] == 1000.0
    assert confirmed['message_key'] == facts[0]['message_key']
    assert pending.progress_draft['source_revision'] == 1  # 新事件只在核实身份后进入后续编辑。
    case.final = '完整最终回复'
    assert case.worker.run_once() == 1 and not case.store.pending()
    assert len(case.finals) == 1


def test_b1_unknown_progress_outside_window_keeps_original_facts_without_provider_io(tmp_path, monkeypatch):
    case = _card_worker(tmp_path, monkeypatch, unknown=True)
    case.worker.run_once()
    original = case.store.pending()[0].progress_draft['component_receipts'][0]
    case.now += 3600
    case.worker.run_once()
    assert len(case.calls) == 1 and len(case.logical_cards) == 1
    pending = case.store.pending()[0]
    assert pending.progress_draft['disabled'] and not pending.progress_draft['reconcile_pending']
    assert pending.progress_draft['component_receipts'][0] == original
    case.final = '最终结果不受未知进度阻塞'
    assert case.worker.run_once() == 1
    assert len(case.finals) == 1
    assert case.store.terminal_receipt('card-request')['progress_draft']['component_receipts'][0] == original


@pytest.mark.parametrize('fail_edits', [False, True])
def test_b1_terminal_tool_events_get_one_bounded_same_card_edit_before_final(tmp_path, monkeypatch, fail_edits):
    case = _card_worker(tmp_path, monkeypatch, fail_edits=fail_edits)
    case.worker.run_once()
    case.now += 0.1  # 终态有界更新不能被普通一秒节流吞掉。
    case.revision, case.final = 10, '最终业务结果'
    assert case.worker.run_once() == 1
    patches = [row for row in case.calls if row[0] == 'PATCH']
    assert len(patches) == 1 and patches[0][1].endswith('/om_card_original')
    card = json.loads(patches[0][2]['content'])
    assert card['body']['elements'][0]['elements'][0]['content'].count('完成') == 5
    assert len(case.logical_cards) == 1 and len(case.finals) == 1
    assert case.store.terminal_receipt('card-request')['disposition'] == 'sent'


def test_b1_card_edit_failure_does_not_block_final_or_change_task_outcome(tmp_path, monkeypatch):
    case = _card_worker(tmp_path, monkeypatch, fail_edits=True)
    case.worker.run_once()
    case.now += 2
    case.revision = 10
    case.worker.run_once()
    assert any(method == 'PATCH' for method, _, _ in case.calls)
    case.final = '最终业务结果'
    assert case.worker.run_once() == 1
    assert case.store.terminal_receipt('card-request')['disposition'] == 'sent'
    assert len(case.finals) == 1


def test_old_text_identity_is_not_migrated_in_place_to_card(tmp_path, monkeypatch):
    case = _card_worker(tmp_path, monkeypatch)
    original = case.store.pending()[0]
    old = replace(original, progress_draft={'mode': 'stream', 'message_id': 'om_old_text', 'content': '旧文本'})
    assert case.store.compare_and_swap(original, old)
    case.worker.run_once()
    assert len(case.calls) == 0  # 无文字的卡片批次不能把旧 text 迁成卡片。
    assert case.store.pending()[0].progress_draft.get('render_kind', 'text') == 'text'


def test_real_tool_projection_and_renderer_are_pure_bounded_and_unknown_safe():
    assert project_execution_steps([{'kind': 'assistant_commentary', 'text': '已完成全部工具'}], request_id='r') == ()
    snapshot = {'steps': [{'step_id': str(index), 'tool_name': 'tool', 'phase': 'finished'} for index in range(70)],
        'truncated': True}
    before = json.dumps(snapshot, sort_keys=True)
    card = render_progress_card(snapshot)
    text = card['body']['elements'][0]['elements'][0]['content']
    assert text.count('状态未确认') == 50 and '其余已折叠' in text
    assert json.dumps(snapshot, sort_keys=True) == before
    assert '完成比例' not in text


def test_manager_freezes_policy_and_provider_facts_in_original_pending_record(monkeypatch):
    from agent_py_agent.agent.adapter.manager import GatewayAskSubmission
    from agent_py_agent.agent.adapter.protocol import IncomingMessage

    manager = ChannelManager(reply_policy='thread', progress_render_kind='card')
    adapter = feishu.FeishuAdapter({'feishu_session_lock_enabled': False})
    adapter._get_tenant_access_token = lambda: None
    manager.register_adapter(adapter)
    manager._submit_gateway_payload = lambda *_: GatewayAskSubmission('request-frozen')
    message = IncomingMessage('feishu', 'ou_actor', '正文', 'om_incoming', conversation_id='oc_group:thread:omt_provider',
        metadata={'feishu_chat_type': 'group', 'feishu_chat_id': 'oc_group', 'feishu_thread_id': 'omt_provider',
            'feishu_root_id': 'om_root', 'feishu_parent_id': 'om_parent'})
    assert manager.route_message(message)
    ingress = manager._delivery_worker.store.records()[0]
    assert manager._delivery_worker._process_record(ingress, 1000) == 1
    pending = manager._reply_delivery.store.pending()[0]
    assert pending.reply_policy == 'thread' and pending.message_id == 'om_incoming'
    assert (pending.provider_thread_id, pending.root_id, pending.parent_id) == ('omt_provider', 'om_root', 'om_parent')
    assert pending.conversation_id != pending.provider_thread_id
    manager._reply_policy = 'reference'
    assert manager._reply_delivery.store.pending()[0].reply_policy == 'thread'


def test_terminal_progress_preserves_structured_quarantine_instead_of_treating_it_as_display_io(tmp_path, monkeypatch):
    case = _card_worker(tmp_path, monkeypatch)
    case.worker.run_once()

    def quarantined(_record):
        raise delivery.GatewayReplyQuarantineError('original_binding_conflict', category='config')

    case.worker._poll_progress = quarantined
    case.final = '不能绕过原目标绑定的最终结果'
    assert case.worker.run_once() == 0
    assert not case.finals
    assert case.store.terminal_receipt('card-request')['disposition'] == 'quarantined'
