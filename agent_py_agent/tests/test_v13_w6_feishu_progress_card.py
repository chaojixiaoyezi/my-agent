"""SYNTHETIC_ONLY：真实worker持久化+统一DeliveryService；无飞书网络。"""
from __future__ import annotations

import copy
import json
from types import SimpleNamespace

from agent_py_agent.agent.adapter.delivery import (
    GatewayReplyDeliveryStore,
    GatewayReplyDeliveryWorker,
    PendingGatewayReply,
)
from agent_py_agent.agent.adapter.feishu_progress_card import (
    MAX_CARD_JSON_BYTES,
    render_progress_card,
)
from agent_py_agent.agent.adapter.manager import ChannelManager
from agent_py_agent.agent.adapter.progress_draft import (
    GatewayProgressBatch,
    project_execution_steps,
    send_gateway_draft,
)
from agent_py_agent.agent.delivery import (
    ChannelAdapterRegistry,
    ChannelCapabilities,
    DeliveryService,
)


def _steps(count):
    return tuple({'step_id': f'SYNTHETIC_ONLY_CALL_{i}', 'tool_name': 'read_file', 'phase': 'finished',
                  'ok': True, 'handler_executed': True, 'duration_ms': i, 'label': f'步骤{i}'} for i in range(count))


def _panel(card):
    return next(row for row in card['body']['elements'] if row['tag'] == 'collapsible_panel')


def _text(card):
    return json.dumps(card, ensure_ascii=False)


def test_latest_50_steps_and_footer_are_byte_bounded():
    snapshot = {'steps': _steps(60), 'request_phase': 'updated', 'total_steps': 60}
    before = copy.deepcopy(snapshot)
    card = render_progress_card(snapshot)
    rows = _panel(card)['elements']
    assert len(rows) == 50
    assert 'SYNTHETIC_ONLY_CALL_10' in _text(rows[0])
    assert 'SYNTHETIC_ONLY_CALL_59' in _text(rows[-1])
    assert '10 条更早' in _text(card)
    assert any(row.get('element_id') == 'status_footer' for row in card['body']['elements'])
    assert len(json.dumps(card).encode()) <= MAX_CARD_JSON_BYTES
    assert snapshot == before


def test_byte_budget_discards_oldest_not_latest():
    steps = [dict(row, label='中文🙂' * 200, detail_ref='中文🙂' * 200) for row in _steps(60)]
    card = render_progress_card({'steps': steps})
    assert 0 < len(_panel(card)['elements']) < 50
    assert 'SYNTHETIC_ONLY_CALL_59' in _text(_panel(card)['elements'][-1])
    assert len(json.dumps(card).encode()) <= MAX_CARD_JSON_BYTES


def test_legacy_truncated_flag_is_visible_without_inventing_total():
    card = render_progress_card({'steps': _steps(1), 'truncated': True})
    assert '"events_truncated": true' in _text(card).replace('\\"', '"')
    assert '总数未知' in _text(card)


class Adapter:
    supports_edit_progress = True
    supports_progress_cards = True
    provider_idempotent_delivery = True
    provider_idempotency_window_seconds = 3600

    def __init__(self):
        self.created = []
        self.edits = []
        self.finals = []
        self.edit_ok = True

    def start_progress_message(self, target, text, *, reply_to='', idempotency_key='', **options):
        self.created.append((target, text, options))
        return 'om_SYNTHETIC_ONLY_CARD'

    def edit_progress_message(self, message_id, text, **options):
        self.edits.append((message_id, text, options))
        return self.edit_ok

    def send_message(self, target, message):
        self.finals.append((target, message.content))
        return True


def _fixture(tmp_path, monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr('agent_py_agent.agent.adapter.progress_draft.time.time', lambda: clock[0])
    adapter = Adapter()
    registry = ChannelAdapterRegistry()
    registry.register_adapter('feishu', adapter, capabilities=ChannelCapabilities(edit_progress=True))
    service = DeliveryService(registry)
    normal = []
    manager = SimpleNamespace(_delivery_service=service, _adapters={'feishu': adapter},
        _send_gateway_reply=lambda *args: normal.append(args) or True,
        _update_active_channel=lambda *args: None)
    store = GatewayReplyDeliveryStore(tmp_path)
    batch = [GatewayProgressBatch(('执行记录',), 60, 'stream', True, steps=_steps(60), render_kind='card', source_revision=60)]
    final = [None]
    def deliver_response(row, text):
        return ChannelManager._deliver_gateway_reply(manager, row, text)
    # 与生产绑定方法的registry等价，给worker提供真实provider的幂等能力事实。
    deliver_response.delivery_adapter = adapter
    worker = GatewayReplyDeliveryWorker(store, poll_response=lambda _: final[0],
        deliver_response=deliver_response,
        poll_progress=lambda row: batch[0], deliver_progress=lambda row, text: send_gateway_draft(service, row, text))
    worker.enqueue(PendingGatewayReply(request_id='SYNTHETIC_ONLY_REQUEST', channel='feishu', user_id='ou_synthetic',
        message_id='om_synthetic_inbound'))
    return adapter, store, worker, clock, batch, final, normal


def test_first_post_one_id_throttle_then_patch_same_card(tmp_path, monkeypatch):
    adapter, store, worker, clock, batch, _, _ = _fixture(tmp_path, monkeypatch)
    worker.run_once()
    assert len(adapter.created) == 1
    draft = store.pending()[0].progress_draft
    assert draft['message_id'] == 'om_SYNTHETIC_ONLY_CARD'
    card = adapter.created[0][2]['card']
    assert card['schema'] == '2.0' and len(_panel(card)['elements']) == 50
    assert '10 条更早' in _text(card) and '执行中' in _text(card)
    for i in range(3):
        clock[0] += 0.1
        batch[0] = GatewayProgressBatch(('更新',), 61 + i, 'stream', True,
            steps=(dict(_steps(60)[-1], phase='updated'),), render_kind='card', source_revision=61 + i)
        worker.run_once()
    assert not adapter.edits
    clock[0] += 1
    worker.run_once()
    assert len(adapter.edits) == 1 and adapter.edits[0][0] == draft['message_id']
    assert '10 条更早' in _text(adapter.edits[-1][2]['card'])


def test_final_collapses_same_message_and_keeps_full_reply(tmp_path, monkeypatch):
    adapter, store, worker, _, _, final, normal = _fixture(tmp_path, monkeypatch)
    worker.run_once()
    final[0] = 'SYNTHETIC_ONLY完整结论' * 100
    worker.run_once()
    assert len(adapter.created) == 1 and not normal and not store.pending()
    message_id, text, options = adapter.edits[-1]
    assert message_id == 'om_SYNTHETIC_ONLY_CARD' and text == final[0]
    card = options['card']
    assert _panel(card)['expanded'] is False
    assert final[0] in [row.get('text', {}).get('content') for row in card['body']['elements']]
    assert '已完成' in _text(card)


def test_oversized_final_uses_normal_path_without_partial_patch(tmp_path, monkeypatch):
    adapter, _, worker, _, _, final, normal = _fixture(tmp_path, monkeypatch)
    worker.run_once()
    final[0] = 'SYNTHETIC_ONLY中文结论🙂' * 5000
    worker.run_once()
    assert len(normal) == 1 and normal[0][2] == final[0]
    assert not adapter.edits


def test_final_edit_failure_does_not_send_duplicate_normal_reply(tmp_path, monkeypatch):
    adapter, store, worker, _, _, final, normal = _fixture(tmp_path, monkeypatch)
    worker.run_once()
    adapter.edit_ok = False
    final[0] = 'SYNTHETIC_ONLY结论'
    worker.run_once()
    assert not normal and store.pending()


def test_call_id_identity_and_legacy_fallback_unchanged():
    rows = project_execution_steps([{'tool': 'read_file', 'phase': 'started', 'call_id': 'SYNTHETIC_ONLY_C',
        'round': 2, 'call_index': 3}, {'tool': 'read_file', 'phase': 'started', 'round': 2, 'call_index': 4}], request_id='SYNTHETIC_ONLY_R')
    assert [row['step_id'] for row in rows] == ['SYNTHETIC_ONLY_C', 'SYNTHETIC_ONLY_R:2:4']
