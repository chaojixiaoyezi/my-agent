from agent_py_agent.agent.adapter.delivery import GatewayReplyDeliveryStore, PendingGatewayReply
from agent_py_agent.agent.adapter.ingress import GatewayIngressRecord, GatewayIngressStore
from agent_py_agent.agent.adapter.lifecycle_records import adapter_lifecycle_records
from agent_py_agent.agent.adapter.protocol import provider_message_reference


def test_read_missing_is_side_effect_free(tmp_path):
    root = tmp_path / 'missing'
    page = adapter_lifecycle_records(root, allowed_owners=None)
    assert page['coverage']['state'] == 'unavailable'
    assert not root.exists()


def test_canonical_owner_and_redacted_correlation(tmp_path):
    store = GatewayIngressStore(tmp_path)
    record = GatewayIngressRecord(ingress_id='a'*64, channel='feishu', user_id='secret-user',
        conversation_id='secret-chat', provider_message_id='msg1', canonical_payload_digest='x',
        content='secret-body', metadata={'owner_id': 'alice'},
        submission={'request_id': 'req1', 'target_turn_id': 'turn1'}, created_at=1, updated_at=1)
    store.put_if_absent(record)
    page = adapter_lifecycle_records(tmp_path, allowed_owners=frozenset({'alice'}), message_ref=provider_message_reference('msg1'))
    assert page['matches'][0]['target_turn_id'] == 'turn1'
    assert 'secret' not in str(page)
    assert not adapter_lifecycle_records(tmp_path, allowed_owners=frozenset({'bob'}))['matches']


def test_legacy_reply_and_terminal_receipt(tmp_path):
    record = PendingGatewayReply('req', 'feishu', 'user', 'msg', target_turn_id='turn')
    assert PendingGatewayReply.from_dict(record.to_dict()).target_turn_id == 'turn'
    assert PendingGatewayReply.from_dict({'request_id':'req','channel':'feishu','user_id':'user'}).target_turn_id == ''
    store = GatewayReplyDeliveryStore(tmp_path)
    assert store.terminal_receipt_report('req')['delivery_outcome'] == 'unknown'
    store.mark_sent(record)
    assert store.terminal_receipt('req')['target_turn_id'] == 'turn'
    assert store.terminal_receipt_report('req')['state'] == 'sent'
    assert not store._pending_path('req').with_suffix('.json.lock').exists()


def test_hash_collision_is_ambiguous(tmp_path, monkeypatch):
    from agent_py_agent.agent.adapter import protocol
    monkeypatch.setattr(protocol, 'provider_message_reference', lambda value: 'collision' if value else '')
    # Reader imported helper also follows the same deterministic collision fixture.
    from agent_py_agent.agent.adapter import lifecycle_records
    monkeypatch.setattr(lifecycle_records, 'provider_message_reference', protocol.provider_message_reference)
    store = GatewayIngressStore(tmp_path)
    for key, message in [('a', 'one'), ('b', 'two')]:
        store.put_if_absent(GatewayIngressRecord(ingress_id=key*64, channel='feishu', user_id='u',
            conversation_id='c', provider_message_id=message, canonical_payload_digest='digest', content='body',
            metadata={'owner_id': 'alice'}, created_at=1, updated_at=1))
    page = adapter_lifecycle_records(tmp_path, allowed_owners=frozenset({'alice'}), message_ref='collision')
    assert page['coverage']['state'] == 'ambiguous'
    assert page['matches'] == []


def test_sparse_pages_progress_without_other_owner_reads(tmp_path):
    store = GatewayIngressStore(tmp_path)
    for key, owner in [('a', 'bob'), ('b', 'alice')]:
        store.put_if_absent(GatewayIngressRecord(ingress_id=key*64, channel='feishu', user_id='u',
            conversation_id='c', provider_message_id=key, canonical_payload_digest='digest', content='body',
            metadata={'owner_id': owner}, created_at=1, updated_at=1))
    first = adapter_lifecycle_records(tmp_path, allowed_owners=frozenset({'alice'}), limit=1)
    assert not first['matches'] and first['next_cursor']
    second = adapter_lifecycle_records(tmp_path, allowed_owners=frozenset({'alice'}), limit=1, cursor=first['next_cursor'])
    assert second['matches'][0]['owner_id'] == 'alice'
    assert not adapter_lifecycle_records(tmp_path, allowed_owners=frozenset({'bob'}), cursor=first['next_cursor'])['matches']


def test_existing_index_backfill_uses_original_bounded_maintenance(tmp_path):
    import sqlite3
    store = GatewayIngressStore(tmp_path)
    record = GatewayIngressRecord(ingress_id='c'*64, channel='feishu', user_id='u',
        conversation_id='c', provider_message_id='m', canonical_payload_digest='d', content='body',
        metadata={'owner_id': 'alice'}, created_at=1, updated_at=1)
    store.put_if_absent(record)
    index = tmp_path / 'ingress' / 'index.sqlite3'
    with sqlite3.connect(index) as db:
        db.execute('DROP TABLE ingress_correlation')
    reopened = GatewayIngressStore(tmp_path)
    reopened._ensure_dirs()
    before = adapter_lifecycle_records(tmp_path, allowed_owners=frozenset({'alice'}))
    assert before['coverage']['state'] == 'partial'
    for _ in range(4):
        reopened.maintenance()
    after = adapter_lifecycle_records(tmp_path, allowed_owners=frozenset({'alice'}))
    assert after['matches'][0]['ingress_id'] == record.ingress_id
    assert after['coverage']['state'] == 'complete'
