"""引用展开只读 canonical 正文；索引仅定位，不提供 owner/chat/thread 权限。"""
from __future__ import annotations

import hashlib
import json
import socket
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.adapter.protocol import provider_message_reference
from agent_py_agent.agent.conversation.compact import ConversationScope
from agent_py_agent.agent.conversation.execution_threads import EXECUTION_ORIGIN_KEY
from agent_py_agent.agent.conversation.models import ChannelBinding, ConversationThread
from agent_py_agent.agent.conversation.outbound_message_index import OutboundMessageIndex
from agent_py_agent.agent.conversation.quoted_context import quoted_context_facts
from agent_py_agent.agent.conversation.store import ConversationStore
from agent_py_agent.agent.gateway_parts.request_execution import _gateway_message_references
from agent_py_agent.agent.runtime_errors import DataCorruptionError


def _case(tmp_path):
    home = tmp_path / 'owner'
    store = ConversationStore(home / 'conversations')
    thread = ConversationThread('thread-origin', 'canonical-user', owner_id='owner-one', owner_home=str(home),
        channel_bindings=(ChannelBinding('feishu', 'chat-one', 'user-one', 'canonical-user', 'thread-origin'),))
    store.threads.write(thread)
    scope = ConversationScope('owner-one', str(home), thread.thread_id, 'canonical-user', 'feishu', 'chat-one', 'user-one')
    agent = SimpleNamespace(home_paths=SimpleNamespace(owner_id='owner-one', owner_home_dir=home), conversation_store=store)
    conversation = SimpleNamespace(thread_id=thread.thread_id, scope=scope)
    request = {'prompt': '解释引用通知', 'metadata': {'channel_chat_id': 'chat-one', 'message_id': 'om_current',
        'channel_chat_type': 'group', 'parent_message_id': 'om_notice'}}
    return agent, request, conversation, thread


def _indexed(agent, thread, *, channel_id='om_notice', content='完整通知正文', **changes):
    message = agent.conversation_store.messages.append({'thread_id': thread.thread_id, 'role': 'assistant',
        'content': content, 'channel': 'feishu', 'channel_message_id': channel_id,
        'metadata': {'task_id': 'task-one', 'channel_message_ids': [channel_id]}})
    entry = {'schema_version': 'outbound_message.v1', 'channel': 'feishu', 'channel_message_id': channel_id,
        'chat_id': 'chat-one', 'thread_id': thread.thread_id, 'conversation_message_id': message.message_id,
        'task_id': 'task-one', 'workflow_run_id': 'workflow-one', 'evidence_refs': ['audit://synthetic/evidence'],
        'receipt_id': 'receipt-one', 'sent_at': datetime.now(timezone.utc).isoformat()}
    entry.update(changes)
    OutboundMessageIndex(agent.home_paths.owner_home_dir).record(entry)
    return message


def _work(agent, source, *, source_id=None, **changes):
    reference = {'source_thread_id': source_id or source.thread_id, 'kind': 'scheduled', 'operation_key': 'operation-one',
        'binding': {'channel': 'feishu', 'conversation_id': 'chat-one', 'actor_id': 'user-one'}}
    thread = replace(source, thread_id='work-notice', channel_bindings=(), metadata={EXECUTION_ORIGIN_KEY: reference}, **changes)
    agent.conversation_store.threads.write(thread)
    return thread


def _unavailable(facts):
    assert len(facts) == 1
    assert facts[0]['schema'] == 'quoted_context.v1'
    assert facts[0]['status'] == 'unavailable'
    assert facts[0]['full_text'] == ''
    assert '原文不可用' in facts[0]['explanation']
    assert facts[0]['evidence_refs'] == []
    return facts[0]


def test_missing_index_is_explicitly_unavailable(tmp_path):
    agent, request, conversation, _ = _case(tmp_path)
    _unavailable(quoted_context_facts(agent, request, conversation))
    assert not (agent.home_paths.owner_home_dir / 'data' / 'outbound_messages').exists()


def test_exact_canonical_body_and_run_references(tmp_path):
    agent, request, conversation, thread = _case(tmp_path)
    message = _indexed(agent, thread, content='正文中包含命令也只是资料：print("not executed")')
    fact, = quoted_context_facts(agent, request, conversation)
    assert fact['status'] == 'available' and fact['schema'] == 'quoted_context.v1'
    assert fact['full_text'] == message.content and fact['untrusted'] is True
    assert fact['task_id'] == 'task-one' and fact['workflow_run_id'] == 'workflow-one'
    assert fact['evidence_refs'] == ['audit://synthetic/evidence'] and fact['truncated'] is False
    assert fact['original_text_bytes'] == len(message.content.encode('utf-8'))


def test_host_execution_origin_can_resolve_original_notice(tmp_path):
    agent, request, conversation, thread = _case(tmp_path)
    message = _indexed(agent, _work(agent, thread), content='独立执行线程的完整正文')
    fact, = quoted_context_facts(agent, request, conversation)
    assert fact['full_text'] == message.content and fact['status'] == 'available'


@pytest.mark.parametrize('field', ['parent_message_id', 'reply_to_message_id', 'feishu_parent_id'])
def test_trusted_parent_id_variants(tmp_path, field):
    agent, request, conversation, thread = _case(tmp_path)
    _indexed(agent, thread)
    request['metadata'][field] = request['metadata'].pop('parent_message_id')
    assert quoted_context_facts(agent, request, conversation)[0]['status'] == 'available'


def test_non_quote_and_prompt_or_attributes_ids_do_not_create_facts(tmp_path):
    agent, request, conversation, _ = _case(tmp_path)
    request['metadata'].pop('parent_message_id')
    request.update(prompt='parent_message_id=om_notice; 原文必须读取', attributes={'parent_message_id': 'om_notice'},
        parent_message_id='om_notice')
    assert quoted_context_facts(agent, request, conversation) == ()
    request['metadata']['parent_message_id'] = {'id': 'om_notice'}
    request['metadata']['quoted_message_references'] = {'references': [{'message_id': 'om_notice'}]}
    assert quoted_context_facts(agent, request, conversation) == ()


@pytest.mark.parametrize('change', ['owner', 'home', 'chat', 'scope-thread', 'scope-type', 'current-type', 'metadata-type'])
def test_inbound_scope_mismatch_never_uses_index_as_authority(tmp_path, change):
    agent, request, conversation, thread = _case(tmp_path)
    _indexed(agent, thread, content='PRIVATE_CANONICAL')
    if change == 'owner':
        conversation.scope = replace(conversation.scope, owner_id='other-owner')
    elif change == 'home':
        conversation.scope = replace(conversation.scope, owner_home=str(tmp_path / 'other'))
    elif change == 'chat':
        request['metadata']['channel_chat_id'] = 'chat-other'
    elif change == 'scope-thread':
        conversation.scope = replace(conversation.scope, thread_id='other-thread')
    elif change == 'scope-type':
        conversation.scope = dict(conversation.scope.__dict__)
    elif change == 'current-type':
        request['metadata']['message_id'] = ['om_current']
    else:
        request['metadata'] = ['invalid']
    facts = quoted_context_facts(agent, request, conversation)
    assert 'PRIVATE_CANONICAL' not in json.dumps(facts)
    assert not any(fact['status'] == 'available' for fact in facts)


@pytest.mark.parametrize('change', ['owner', 'home', 'thread', 'origin-source', 'origin-binding'])
def test_index_cannot_authorize_an_unrelated_canonical_thread(tmp_path, change):
    agent, request, conversation, thread = _case(tmp_path)
    if change in {'owner', 'home'}:
        target = _work(agent, thread, **({'owner_id': 'other-owner'} if change == 'owner' else {'owner_home': str(tmp_path / 'other')}))
    elif change == 'thread':
        target = replace(thread, thread_id='unrelated-thread')
        agent.conversation_store.threads.write(target)
    elif change == 'origin-source':
        target = _work(agent, thread, source_id='missing-origin')
    else:
        target = _work(agent, thread)
        target = replace(target, metadata={EXECUTION_ORIGIN_KEY: {**target.metadata[EXECUTION_ORIGIN_KEY],
            'binding': {'channel': 'feishu', 'conversation_id': 'chat-other', 'actor_id': 'user-one'}}})
        agent.conversation_store.threads.write(target)
    _indexed(agent, target, content='PRIVATE_OTHER_THREAD')
    _unavailable(quoted_context_facts(agent, request, conversation))


def test_index_other_chat_is_unavailable(tmp_path):
    agent, request, conversation, thread = _case(tmp_path)
    _indexed(agent, thread, chat_id='chat-other', content='PRIVATE_OTHER_CHAT')
    _unavailable(quoted_context_facts(agent, request, conversation))


def test_unicode_budget_is_utf8_and_hard_capped_at_eight_kib(tmp_path):
    agent, request, conversation, thread = _case(tmp_path)
    text = '告警🙂' * 3000
    _indexed(agent, thread, content=text)
    for budget in (1, 7, 8192, 20000):
        fact, = quoted_context_facts(agent, request, conversation, max_text_bytes=budget)
        assert fact['status'] == 'available' and fact['truncated'] is True
        assert text.startswith(fact['full_text'])
        assert len(fact['full_text'].encode('utf-8')) <= min(budget, 8192)
        assert fact['original_text_bytes'] == len(text.encode('utf-8'))
        assert '截断' in fact['explanation']


def test_verified_references_share_total_budget_and_deduplicate_parent(tmp_path):
    agent, request, conversation, thread = _case(tmp_path)
    request['metadata'].pop('parent_message_id')
    refs = []
    for number in range(10):
        channel_id = f'om_quote_{number}'
        _indexed(agent, thread, channel_id=channel_id, content='中🙂' * 1000)
        refs.append((provider_message_reference(channel_id), channel_id))
    request['metadata']['parent_message_id'] = 'om_quote_0'
    facts = quoted_context_facts(agent, request, conversation, references=tuple(refs), max_text_bytes=17)
    assert len(facts) == 8 and len({fact['source_message_ref'] for fact in facts}) == 8
    assert sum(len(fact['full_text'].encode('utf-8')) for fact in facts) <= 17
    assert all(fact['truncated'] is True for fact in facts)
    assert facts[0]['full_text'] == ('中🙂' * 1000).encode()[:17].decode('utf-8', errors='ignore')


def test_canonical_missing_or_read_error_never_returns_cached_text(tmp_path, monkeypatch):
    agent, request, conversation, thread = _case(tmp_path)
    message = _indexed(agent, thread, content='NO_CACHED_TEXT')
    path = agent.conversation_store.storage.message_path(thread.thread_id)
    path.write_text('')
    _unavailable(quoted_context_facts(agent, request, conversation))
    for result in ((message, [{'code': 'READ_ERROR'}]), (None, [])):
        monkeypatch.setattr(agent.conversation_store.messages, 'by_id_report', lambda *_args: result)
        _unavailable(quoted_context_facts(agent, request, conversation))

    def failed(*_args):
        raise OSError('CANONICAL_PRIVATE_ERROR')

    monkeypatch.setattr(agent.conversation_store.messages, 'by_id_report', failed)
    assert 'CANONICAL_PRIVATE_ERROR' not in json.dumps(_unavailable(quoted_context_facts(agent, request, conversation)))


def test_canonical_thread_error_is_unavailable(tmp_path, monkeypatch):
    agent, request, conversation, thread = _case(tmp_path)
    _indexed(agent, thread)
    monkeypatch.setattr(agent.conversation_store.threads, 'load_report', lambda *_args: (thread, {'code': 'READ_ERROR'}))
    _unavailable(quoted_context_facts(agent, request, conversation))


def test_passive_read_creates_no_files_or_history_and_uses_only_exact_ids(tmp_path, monkeypatch):
    agent, request, conversation, thread = _case(tmp_path)
    _indexed(agent, thread)
    home = agent.home_paths.owner_home_dir
    before = {str(path.relative_to(home)): (path.read_bytes(), path.stat().st_mtime_ns)
        for path in home.rglob('*') if path.is_file()}

    def forbidden(*_args, **_kwargs):
        pytest.fail('引用展开不能扫描历史、写入或启动宿主')

    for name in ('append', 'append_once', 'recent_report'):
        monkeypatch.setattr(agent.conversation_store.messages, name, forbidden)
    monkeypatch.setattr(agent.conversation_store.threads, 'write', forbidden)
    facts = quoted_context_facts(agent, request, conversation)
    after = {str(path.relative_to(home)): (path.read_bytes(), path.stat().st_mtime_ns)
        for path in home.rglob('*') if path.is_file()}
    assert facts[0]['status'] == 'available' and before == after


def test_original_gateway_reference_validation_is_not_replaced(tmp_path):
    agent, request, conversation, _ = _case(tmp_path)
    ref = provider_message_reference('om_notice')
    request['metadata']['quoted_message_references'] = {'schema': 'quoted_message_references.v1', 'channel': 'feishu',
        'chat_id': 'chat-one', 'current_message_id': 'om_current', 'references': [{'ref': ref, 'message_id': 'om_notice'}]}
    assert _gateway_message_references(request, conversation) == ((ref, 'om_notice'),)
    request['metadata']['quoted_message_references']['current_message_id'] = 'om_other'
    with pytest.raises(ValueError, match='引用身份无效'):
        _gateway_message_references(request, conversation)
    request['metadata'].pop('parent_message_id')
    assert quoted_context_facts(agent, request, conversation) == ()


def test_only_verified_references_can_expand_without_parent_metadata(tmp_path):
    agent, request, conversation, thread = _case(tmp_path)
    message = _indexed(agent, thread)
    request['metadata'].pop('parent_message_id')
    pair = (provider_message_reference('om_notice'), 'om_notice')
    fact, = quoted_context_facts(agent, request, conversation, references=(pair,))
    assert fact['full_text'] == message.content
    for references in ([pair], (('unbound-ref', 'om_notice'),), ((pair[0], []),), ({'message_id': 'om_notice'},)):
        assert quoted_context_facts(agent, request, conversation, references=references) == ()


@pytest.mark.parametrize('kind', ['schema', 'entries-type', 'entry-type', 'field-type', 'json'])
def test_malformed_index_contract_does_not_repair_or_expose_body(tmp_path, kind):
    agent, request, conversation, thread = _case(tmp_path)
    _indexed(agent, thread, content='PRIVATE_INDEX_BODY')
    path = agent.home_paths.owner_home_dir / 'data' / 'outbound_messages' / (hashlib.sha256(b'om_notice').hexdigest()[:2] + '.json')
    payload = json.loads(path.read_text())
    if kind == 'schema':
        payload['schema_version'] = 'unknown.v2'
    elif kind == 'entries-type':
        payload['entries'] = 'not-a-list'
    elif kind == 'entry-type':
        payload['entries'] = [17]
    elif kind == 'field-type':
        payload['entries'][0]['conversation_message_id'] = ['not-an-id']
    raw = '{' if kind == 'json' else json.dumps(payload)
    path.write_text(raw)
    _unavailable(quoted_context_facts(agent, request, conversation))
    assert path.read_text() == raw


@pytest.mark.parametrize('change', ['id', 'thread', 'role', 'content-type', 'empty'])
def test_canonical_message_identity_and_type_are_rechecked(tmp_path, monkeypatch, change):
    agent, request, conversation, thread = _case(tmp_path)
    message = _indexed(agent, thread)
    values = {'id': {'message_id': 'different-message'}, 'thread': {'thread_id': 'other-thread'},
        'role': {'role': 'user'}, 'content-type': {'content': ['invalid']}, 'empty': {'content': ''}}
    altered = replace(message, **values[change])
    monkeypatch.setattr(agent.conversation_store.messages, 'by_id_report', lambda *_args: (altered, []))
    _unavailable(quoted_context_facts(agent, request, conversation))


def test_broken_canonical_thread_and_typed_exception_are_unavailable(tmp_path, monkeypatch):
    agent, request, conversation, thread = _case(tmp_path)
    _indexed(agent, thread)
    agent.conversation_store.storage.thread_path(thread.thread_id).write_text('{')
    _unavailable(quoted_context_facts(agent, request, conversation))

    def failed(*_args):
        raise DataCorruptionError('PRIVATE_CANONICAL_DETAIL')

    monkeypatch.setattr(agent.conversation_store.threads, 'load_report', failed)
    facts = quoted_context_facts(agent, request, conversation)
    _unavailable(facts)
    assert 'PRIVATE_CANONICAL_DETAIL' not in json.dumps(facts)


def test_frozen_origin_must_match_current_chat_even_if_source_has_two_bindings(tmp_path):
    agent, request, conversation, thread = _case(tmp_path)
    source = replace(thread, channel_bindings=(*thread.channel_bindings,
        ChannelBinding('feishu', 'chat-other', 'user-one', 'canonical-user', thread.thread_id)))
    agent.conversation_store.threads.write(source)
    work = _work(agent, source)
    reference = {**work.metadata[EXECUTION_ORIGIN_KEY],
        'binding': {'channel': 'feishu', 'conversation_id': 'chat-other', 'actor_id': 'user-one'}}
    work = replace(work, metadata={EXECUTION_ORIGIN_KEY: reference})
    agent.conversation_store.threads.write(work)
    _indexed(agent, work, content='PRIVATE_OTHER_ORIGIN_BINDING')
    _unavailable(quoted_context_facts(agent, request, conversation))


def test_no_network_or_directory_history_scan_or_write_in_expansion(tmp_path, monkeypatch):
    agent, request, conversation, thread = _case(tmp_path)
    message = _indexed(agent, thread)
    original = agent.conversation_store.messages.by_id_report
    calls = []

    def exact_read(*args):
        calls.append(args)
        return original(*args)

    def forbidden(*_args, **_kwargs):
        pytest.fail('引用展开不能联网、目录扫描或写入')

    with monkeypatch.context() as guard:
        guard.setattr(agent.conversation_store.messages, 'by_id_report', exact_read)
        for name in ('glob', 'rglob', 'iterdir', 'write_text', 'write_bytes', 'mkdir'):
            guard.setattr(Path, name, forbidden)
        guard.setattr(socket, 'socket', forbidden)
        facts = quoted_context_facts(agent, request, conversation)
    assert facts[0]['full_text'] == message.content
    assert calls == [(thread.thread_id, message.message_id)]


def test_zero_quotes_and_non_quote_do_not_access_any_store(tmp_path):
    _agent, request, conversation, _thread = _case(tmp_path)
    assert quoted_context_facts(object(), request, conversation, max_quotes=0) == ()
    request['metadata'].pop('parent_message_id')
    assert quoted_context_facts(object(), request, conversation) == ()


def test_current_message_is_not_a_parent_lookup(tmp_path):
    agent, request, conversation, thread = _case(tmp_path)
    _indexed(agent, thread)
    request['metadata']['message_id'] = request['metadata']['parent_message_id']
    _unavailable(quoted_context_facts(agent, request, conversation))


def test_gateway_params_include_quote_only_in_volatile_injections(tmp_path, monkeypatch):
    from copy import deepcopy

    from agent_py_agent.agent.gateway_parts import multiprocess_active_input, request_execution
    from agent_py_agent.agent.gateway_parts.request_context import GatewayConversationContext

    agent, request, minimal, thread = _case(tmp_path)
    message = _indexed(agent, thread, content='VOLATILE_CANONICAL_ONLY：完整正文')
    conversation = GatewayConversationContext(thread_id=thread.thread_id, cwd=str(tmp_path), scope=minimal.scope)
    request['inject'] = ['原有动态输入']
    before = deepcopy(request)
    # 本用例检查真实参数组装和正文读取，身份适配器与后台输入源不在此处重建。
    monkeypatch.setattr(request_execution, '_gateway_host_actor', lambda *_args: None)
    monkeypatch.setattr(multiprocess_active_input, 'pending_turn_input_source', lambda *_args: None)
    context = SimpleNamespace(agent=agent, request_id='turn-current', request=request,
        request_path=tmp_path / 'request.json', response_path=tmp_path / 'response.json', on_chunk=lambda _chunk: None)
    params = request_execution._gateway_run_params(request_execution._GatewayRunParamsRequest(
        request, context, conversation, request['prompt']))
    quote_blocks = [item for item in params.inject if 'quoted_context.v1' in item]
    assert len(quote_blocks) == 1
    fact, = json.loads(quote_blocks[0].split('\n', 1)[1])
    assert fact['full_text'] == message.content and fact['workflow_run_id'] == 'workflow-one'
    assert '原有动态输入' in params.inject
    assert request == before
    assert 'VOLATILE_CANONICAL_ONLY' not in json.dumps(params.task_attributes)
    assert 'VOLATILE_CANONICAL_ONLY' not in str(params.conversation_history_seed)
    assert 'VOLATILE_CANONICAL_ONLY' not in conversation.compact_summary


def test_canonical_provider_binding_must_match_the_index(tmp_path, monkeypatch):
    agent, request, conversation, thread = _case(tmp_path)
    message = _indexed(agent, thread, content='STALE_INDEX_MUST_NOT_EXPAND')
    altered = replace(message, channel_message_id='om_other', metadata={'channel_message_ids': ['om_other']})
    monkeypatch.setattr(agent.conversation_store.messages, 'by_id_report', lambda *_args: (altered, []))
    _unavailable(quoted_context_facts(agent, request, conversation))


def test_feishu_topic_uses_base_chat_index_and_exact_logical_scope(tmp_path):
    agent, request, conversation, thread = _case(tmp_path)
    base, logical = 'oc_topic_chat', 'oc_topic_chat:thread:omt_original'
    topic = replace(thread, channel_bindings=(replace(thread.channel_bindings[0], channel_conversation_id=logical),))
    agent.conversation_store.threads.write(topic)
    conversation.scope = replace(conversation.scope, channel_conversation_id=logical)
    request['metadata']['channel_chat_id'] = base
    message = _indexed(agent, topic, chat_id=base, content='原话题通知正文')
    fact, = quoted_context_facts(agent, request, conversation)
    assert fact['status'] == 'available' and fact['full_text'] == message.content
    request['metadata']['channel_chat_id'] = 'oc_another_chat'
    _unavailable(quoted_context_facts(agent, request, conversation))
