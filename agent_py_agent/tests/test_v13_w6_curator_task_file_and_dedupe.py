"""SYNTHETIC_ONLY：任务指针关联、广播去重和增量规则的隔离回归。"""
from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.conversation import ConversationStore
from agent_py_agent.agent.memory_store.candidate_models import CandidateObservation, MemoryScope
from agent_py_agent.agent.memory_store.candidates import CandidateService
from agent_py_agent.agent.memory_store.curator_commit import CuratorBatchCommitter
from agent_py_agent.agent.memory_store.curator_inputs import (
    CuratorMessageSource,
    CuratorToolReferenceSource,
    collect_curator_inputs,
)
from agent_py_agent.agent.memory_store.curator_models import MemoryCuratorConfig, MemoryCuratorState
from agent_py_agent.agent.memory_store.daily import DailyMemoryEvent, DailyMemoryStore
from agent_py_agent.agent.settings import AgentConfig


def _config(**changes):
    return replace(MemoryCuratorConfig(), **changes)


def _store_messages(root, count=1, *, text='SYNTHETIC_ONLY_RULE', channel='tui', created_at=6000.0):
    store = ConversationStore(root / 'conversations')
    messages = []
    for i in range(count):
        thread = store.threads.get_or_create({'canonical_user_id': 'SYNTHETIC_ONLY_USER', 'channel': channel,
            'channel_conversation_id': f'SYNTHETIC_ONLY_CHAT_{i}', 'channel_user_id': 'SYNTHETIC_ONLY_USER', 'now': created_at})
        messages.append(store.messages.append({'thread_id': thread.thread_id, 'role': 'user', 'channel': channel,
            'content': text, 'now': created_at + i, 'metadata': {'request_id': f'SYNTHETIC_ONLY_REQ_{i}',
            'dedupe_key': f'SYNTHETIC_ONLY_BROADCAST:{i}'}}))
    return store, messages


def _collect(root, store, *, tool_source=None, config=None):
    return collect_curator_inputs(message_source=CuratorMessageSource(store), audit_dir=root / 'audit',
        state=MemoryCuratorState(), config=config or _config(), tool_reference_source=tool_source)


def _artifact(root, message, path, *, ok=True, request_id=None, tool='read_file', body=None):
    output = body or ('SYNTHETIC_ONLY任务规则：重要改动独立复核。\n' * 500)
    digest = hashlib.sha256(output.encode()).hexdigest()
    call = 'SYNTHETIC_ONLY_CALL'
    request_id = request_id or message.metadata['request_id']
    artifact = root / 'frozen' / 'SYNTHETIC_ONLY_READ.json'
    artifact.parent.mkdir(parents=True, exist_ok=True)
    payload = {'kind': 'tool_output', 'tool': tool, 'call_id': call, 'run_id': 'SYNTHETIC_ONLY_RUN',
        'request_id': request_id, 'ok': ok, 'parameters': {'path': str(path)}, 'content': output,
        'sha256': digest, 'size_bytes': len(output.encode())}
    artifact.write_text(json.dumps(payload))
    audit = {'event_id': 'SYNTHETIC_ONLY_AUDIT', 'event_type': 'tool_call', 'tool': tool, 'tool_call_id': call,
        'tool_success': ok, 'status': 'ok' if ok else 'failed', 'effect_outcome': 'confirmed' if ok else 'failed',
        'thread_id': message.thread_id, 'request_id': request_id, 'run_id': 'SYNTHETIC_ONLY_RUN',
        'created_at': '1970-01-01T01:40:30+00:00'}
    audit_dir = root / 'audit'
    audit_dir.mkdir(exist_ok=True)
    (audit_dir / '1970-01-01.jsonl').write_text(json.dumps(audit) + '\n')
    row = {**payload, 'path': str(artifact)}
    source = CuratorToolReferenceSource(root, lambda owner, runs, limit: {'ok': True,
        'tool_outputs_by_run': {'SYNTHETIC_ONLY_RUN': [row]}})
    return source, artifact, digest


def test_task_file_evidence_attached_bounded(tmp_path):
    path = tmp_path / 'SYNTHETIC_ONLY_policy.md'
    path.write_text('已经变化的文件不该被策展器重新读取')
    store, messages = _store_messages(tmp_path, text=f'请完整阅读 `{path}` 并照做。')
    source, _, digest = _artifact(tmp_path, messages[0], path)
    batch = _collect(tmp_path, store, tool_source=source)
    evidence = getattr(batch.messages[0], 'referenced_files', ())
    assert len(evidence) == 1
    item = evidence[0]
    assert item['path'] == str(path.resolve()) and item['sha256'] == digest
    assert len(item['excerpt']) == 4000 and item['tool_call_id'] == 'SYNTHETIC_ONLY_CALL'
    assert '已经变化' not in item['excerpt']
    assert item['origin'] == 'user_referenced_file'
    assert batch.to_model_payload()['messages'][0]['referenced_files'] == list(evidence)


@pytest.mark.parametrize('mode', ['wrong_path', 'failed', 'other_request', 'other_tool', 'tampered', 'outside_owner'])
def test_unmatched_failed_or_untrusted_read_has_no_evidence(tmp_path, mode):
    path = tmp_path / 'SYNTHETIC_ONLY_policy.md'
    store, messages = _store_messages(tmp_path, text=f'完整阅读 `{path}`。')
    read_path = path.with_name('SYNTHETIC_ONLY_other.md') if mode == 'wrong_path' else path
    source, artifact, _ = _artifact(tmp_path, messages[0], read_path, ok=mode != 'failed',
        request_id='SYNTHETIC_ONLY_OTHER_REQ' if mode == 'other_request' else None,
        tool='run_command' if mode == 'other_tool' else 'read_file')
    if mode == 'tampered':
        payload = json.loads(artifact.read_text())
        payload['content'] += 'SYNTHETIC_ONLY_TAMPER'
        artifact.write_text(json.dumps(payload))
    if mode == 'outside_owner':
        source = replace(source, owner_root=tmp_path / 'SYNTHETIC_ONLY_OTHER_OWNER')
    batch = _collect(tmp_path, store, tool_source=source)
    assert not getattr(batch.messages[0], 'referenced_files', ())


def test_task_file_custom_budget_and_overall_budget(tmp_path):
    path = tmp_path / 'SYNTHETIC_ONLY_policy.md'
    store, messages = _store_messages(tmp_path, text=f'完整阅读 {path} 并照做。')
    source, _, _ = _artifact(tmp_path, messages[0], path)
    batch = _collect(tmp_path, store, tool_source=source, config=_config(task_file_evidence_max_chars=71))
    assert len(getattr(batch.messages[0], 'referenced_files', ())) == 1
    assert len(batch.messages[0].referenced_files[0]['excerpt']) == 71
    assert len(json.dumps(batch.to_model_payload(), ensure_ascii=False)) <= 40000


# 走真实配置快照和收集入口，不能靠给测试替身临时加属性绕过配置接线。
@pytest.mark.parametrize('budget', [0, 71, 40000])
def test_agent_config_file_evidence_budget_reaches_collect(tmp_path, budget):
    path = tmp_path / 'SYNTHETIC_ONLY_policy.md'
    store, messages = _store_messages(tmp_path, text=f'完整阅读 `{path}` 并照做。')
    body = 'SYNTHETIC_ONLY_FROZEN_RULE\n' * 500
    source, _, digest = _artifact(tmp_path, messages[0], path, body=body)
    config = MemoryCuratorConfig.from_agent_config(AgentConfig(
        memory_curator_task_file_evidence_max_chars=budget))
    batch = _collect(tmp_path, store, tool_source=source, config=config)
    assert len(batch.messages) == 1
    evidence = batch.messages[0].referenced_files
    if budget == 0:
        assert evidence == ()
    else:
        assert len(evidence) == 1
        assert evidence[0]['excerpt'] == body[:budget]
        assert evidence[0]['sha256'] == digest
    assert config.task_file_evidence_max_chars == budget


@pytest.mark.parametrize(('window', 'created_at', 'model_messages'), [
    (0, 6059.0, 2), (60, 6059.0, 2), (600, 6059.0, 1), (86400, 6599.0, 1),
])
def test_agent_config_dedupe_window_reaches_collect(tmp_path, window, created_at, model_messages):
    # 6059/6060 跨60秒桶，但仍处于同一个600秒桶；原始消息与游标必须始终保留两条。
    store, messages = _store_messages(tmp_path, 2, created_at=created_at)
    config = MemoryCuratorConfig.from_agent_config(AgentConfig(
        memory_curator_cross_thread_dedupe_window_seconds=window))
    batch = _collect(tmp_path, store, config=config)
    assert {message.message_id for message in batch.messages} == {message.message_id for message in messages}
    assert len(batch.to_model_payload()['messages']) == model_messages
    if window == 0:
        assert all(not message.broadcast for message in batch.messages)
    assert getattr(config, 'cross_thread_dedupe_window_seconds', 600) == window


def test_agent_config_default_revision_preserves_legacy_state():
    # 修复前真实默认快照的摘要；新默认字段不能使既有state误判为配置切换。
    assert MemoryCuratorConfig().revision() == 'curator-config-2a1d051db3fe348b'
    assert MemoryCuratorConfig.from_agent_config(AgentConfig()).revision() == MemoryCuratorConfig().revision()


@pytest.mark.parametrize(('field', 'value', 'default'), [
    ('task_file_evidence_max_chars', 0, 4000), ('task_file_evidence_max_chars', 71, 4000),
    ('cross_thread_dedupe_window_seconds', 0, 600), ('cross_thread_dedupe_window_seconds', 60, 600),
])
def test_agent_config_nondefault_revision_changes_and_restores(field, value, default):
    config = MemoryCuratorConfig.from_agent_config(AgentConfig(**{f'memory_curator_{field}': value}))
    assert config.revision() != MemoryCuratorConfig().revision()
    assert replace(config, **{field: default}).revision() == MemoryCuratorConfig().revision()


def test_real_tool_archive_and_control_plane_feed_task_evidence(tmp_path):
    from agent_py_agent.agent.memory_archive.control_plane import query_tool_output_refs_for_runs
    from agent_py_agent.agent.memory_archive.tool_output_externalizer import (
        ExternalizeToolOutputRequest,
        externalize_tool_output_record,
    )

    path = tmp_path / 'SYNTHETIC_ONLY_rule.md'
    body = 'SYNTHETIC_ONLY正式规则：独立复核。'
    store, messages = _store_messages(tmp_path, text=f'请读 `{path}` 并照做。')
    message = messages[0]
    call, run = 'SYNTHETIC_ONLY_REAL_CALL', 'SYNTHETIC_ONLY_REAL_RUN'
    record = externalize_tool_output_record(ExternalizeToolOutputRequest(root=tmp_path, tool='read_file',
        call_id=call, output=body, ok=True, request_id=message.metadata['request_id'], run_id=run,
        parameters={'path': str(path), 'start_line': 1, 'end_line': 1}, force_externalize=True))
    audit = {'event_id': 'SYNTHETIC_ONLY_REAL_AUDIT', 'event_type': 'tool_call', 'tool': 'read_file',
        'tool_call_id': call, 'tool_success': True, 'status': 'ok', 'effect_outcome': 'confirmed',
        'thread_id': message.thread_id, 'request_id': message.metadata['request_id'], 'run_id': run,
        'created_at': '1970-01-01T01:40:30+00:00'}
    (tmp_path / 'audit').mkdir()
    (tmp_path / 'audit' / '1970-01-01.jsonl').write_text(json.dumps(audit) + '\n')
    source = CuratorToolReferenceSource(tmp_path, lambda owner, runs, limit:
        query_tool_output_refs_for_runs(owner, run_ids=runs, limit_per_run=limit))
    batch = _collect(tmp_path, store, tool_source=source)
    [evidence] = batch.messages[0].referenced_files
    assert evidence['excerpt'] == body and evidence['tool_call_id'] == call
    assert evidence['artifact_ref'] == record['artifact_ref']
    assert evidence['sha256'] == hashlib.sha256(body.encode()).hexdigest()
    assert evidence['hash_scope'] == 'verified_read_output'


def test_broadcast_actual_curator_runs_commit_one_daily_candidate_and_all_cursors(tmp_path, monkeypatch):
    from agent_py_agent.agent.backends import ModelResponse
    from agent_py_agent.tests.curator_slice_test_support import expire_first_thread_read
    from agent_py_agent.tests.test_curator_observation_identity import _candidate, _output, _service

    store, messages = _store_messages(tmp_path, 12)
    by_id = {item.message_id: item for item in messages}

    class Backend:
        name = 'actual-batch-broadcast'
        calls = 0
        batches = []

        def generate_structured(self, prompt, *, response_schema):
            # 正式输入可能为任意合法短页；模型只回显当拍实际提供的结构化来源。
            payload = json.loads(prompt.rsplit('待提炼经历 JSON：\n', 1)[1])
            ids = [source['message_id'] for row in payload['messages']
                   for source in row.get('broadcast_sources', [row])]
            assert ids and len(ids) <= 6 and all(mid in by_id for mid in ids)
            self.calls += 1
            self.batches.append(ids)
            refs = [{'message_id': mid} for mid in ids]
            first = by_id[ids[0]]
            candidate = _candidate(first.message_id, 'SYNTHETIC_ONLY_RULE', 'SYNTHETIC_ONLY_SUBJECT')
            candidate.update(candidate_type='user_preference', source_message_refs=refs, promotion_target='user')
            output = _output(first, [candidate])
            output['processed_message_refs'] = refs
            output['daily_events'] = [{'event_type': 'decision', 'summary': 'SYNTHETIC_ONLY_RULE',
                'actor': 'user', 'origin': 'user_explicit', 'message_refs': refs, 'tool_refs': [],
                'artifact_refs': [], 'decisions': [], 'lessons': [], 'next_actions': []}]
            return ModelResponse(text=json.dumps(output), backend=self.name)

    backend = Backend()
    service = _service(tmp_path, backend, store)
    service.config = replace(service.config, batch_message_limit=6)
    fired = expire_first_thread_read(monkeypatch, store)
    expected = {item.thread_id: item.message_id for item in messages}
    results = []
    for _ in range(32):
        result = service.run(reason='admin')
        results.append(result)
        assert result.status in {'succeeded', 'busy'} and not result.failure_code, result
        assert 0 <= result.processed_messages <= 6, result
        if service.state_store.load().per_thread_cursors == expected:
            break
        assert result.processed_messages or 'message_scan_partial' in result.warnings, result
    else:
        pytest.fail(f'12条广播未在32片内全部提交: {results!r}')
    assert fired and 'message_scan_partial' in results[0].warnings
    [event] = service.daily_store.list(day='1970-01-01')
    [candidate] = service.candidate_service.list()
    assert event.sequence == 1 and not event.previous_event_id and len(event.message_refs) == 12
    assert candidate.occurrence_count == 1 and len(candidate.source_message_refs) == 12
    assert service.state_store.load().per_thread_cursors == expected
    assert sum(result.processed_messages for result in results) == 12
    assert {mid for batch in backend.batches for mid in batch} == set(by_id)
    assert backend.calls >= 2


def test_broadcast_interleaved_unprocessed_message_stops_contiguous_cursor(tmp_path):
    from agent_py_agent.agent.memory_store.curator_models import CuratorExtraction
    from agent_py_agent.agent.memory_store.curator_validation import (
        next_cursors,
        validate_extraction,
    )

    store, originals = _store_messages(tmp_path, 2)
    middle = store.messages.append({'thread_id': originals[0].thread_id, 'role': 'user', 'channel': 'tui',
        'content': 'SYNTHETIC_ONLY_UNRELATED', 'now': 6002})
    last = store.messages.append({'thread_id': originals[0].thread_id, 'role': 'user', 'channel': 'tui',
        'content': originals[0].content, 'now': 6003})
    batch = _collect(tmp_path, store)
    processed = tuple({'message_id': item.message_id} for item in (*originals, last))
    extraction = CuratorExtraction((), (), processed, (), (), (), {})
    validated = validate_extraction(extraction, batch, owner_id='SYNTHETIC_ONLY_OWNER')
    cursors = next_cursors({}, '', batch, validated)
    assert cursors.per_thread_cursors[originals[0].thread_id] == originals[0].message_id
    assert cursors.per_thread_cursors[originals[1].thread_id] == originals[1].message_id
    assert middle.message_id not in cursors.per_thread_cursors.values() and cursors.processed_messages == 2


def test_rule_notice_exact_budget_does_not_read_next_notice(tmp_path):
    from agent_py_agent.agent.memory_store.rule_notices import RuleNoticeStore

    store = RuleNoticeStore(tmp_path)
    first, second = 'SYNTHETIC_ONLY_A', 'SYNTHETIC_ONLY_B'
    for index, text in enumerate((first, second)):
        store.publish(memory_id=f'SYNTHETIC_ONLY_RULE_{index}', version=1, text=text)
    assert store.take('SYNTHETIC_ONLY_THREAD', max_chars=len(first)) == first
    assert store.take('SYNTHETIC_ONLY_THREAD', max_chars=len(second)) == second
    assert store.take('SYNTHETIC_ONLY_THREAD') == ''


def test_rule_notice_large_valid_publications_remain_self_readable(tmp_path):
    from agent_py_agent.agent.memory_store.rule_notices import RuleNoticeStore

    store = RuleNoticeStore(tmp_path)
    text = 'SYNTHETIC_ONLY_' + '界' * (40000 - len('SYNTHETIC_ONLY_'))
    for index in range(64):
        store.publish(memory_id=f'SYNTHETIC_ONLY_RULE_{index}', version=1, text=text, max_chars=40000)
    assert store.path.stat().st_size <= 2_097_152
    rows = store._read(store.path)['notices']
    assert rows and rows[-1]['memory_id'] == 'SYNTHETIC_ONLY_RULE_63'
    assert all(len(row['text']) == 40000 for row in rows)
    assert len(store.take('SYNTHETIC_ONLY_THREAD', max_chars=40000)) == 40000


def _persist_candidate_observations(committer, observations):
    changes, committed = committer._candidate_changes(SimpleNamespace(candidate_observations=tuple(observations),
        rejected_target_bindings=()))
    from agent_py_agent.agent.common.json_io import write_private_text_file_atomic_unlocked

    # 真实增量计划先建/写节点再发表唯一头，目录由正式私有writer建立。
    for path, text in changes.items():
        write_private_text_file_atomic_unlocked(path, text)
    return committed


def test_broadcast_12_threads_two_runs_one_daily(tmp_path):
    store, _ = _store_messages(tmp_path, 12)
    batch = _collect(tmp_path, store)
    # 游标仍保留全部原消息，提炼视图才聚合，不丢任何线程的处理身份。
    assert len(batch.messages) == 12
    payload = batch.to_model_payload()
    assert len(payload['messages']) == 1
    assert len(payload['messages'][0]['broadcast_sources']) == 12
    daily = DailyMemoryStore(tmp_path / 'memory' / 'daily')
    committer = object.__new__(CuratorBatchCommitter)
    committer.candidates = CandidateService(tmp_path / 'memory' / 'candidates.jsonl')
    for run, group in enumerate((batch.messages[:6], batch.messages[6:], batch.messages)):
        observations = []
        for message in group:
            ref = {**message.ref(), 'quote': message.full_content}
            daily.append(DailyMemoryEvent('decision', 'SYNTHETIC_ONLY_RULE', 'user', origin='user_explicit',
                message_refs=(ref,), created_at='1970-01-01T01:40:00+00:00', curator_run_id=f'SYNTHETIC_ONLY_RUN_{run}'))
            observations.append(CandidateObservation('user_preference', 'SYNTHETIC_ONLY_RULE', 'SYNTHETIC_ONLY_SUBJECT',
                MemoryScope('personal', 'personal'), 'user_explicit', source_message_refs=(ref,), evidence_refs=(ref,),
                observed_at='1970-01-01T01:40:00+00:00', promotion_target='user'))
        _persist_candidate_observations(committer, observations)
    events = daily.list(day='1970-01-01')
    assert len(events) == 1 and events[0].sequence == 1 and not events[0].previous_event_id
    rows = committer.candidates.readonly_rows()
    assert len(rows) == 1 and rows[0].occurrence_count == 1
    assert len(rows[0].source_message_refs) == 12
    assert len(events[0].message_refs) == 12


def test_different_windows_and_nonuser_sources_are_not_broadcast(tmp_path):
    store, _ = _store_messages(tmp_path, 2, channel='scheduler')
    batch = _collect(tmp_path, store)
    assert len(batch.to_model_payload()['messages']) == 2
    assert all('broadcast_hash' not in message.ref() for message in batch.messages)


def test_broadcast_does_not_collapse_semantic_whitespace(tmp_path):
    store, messages = _store_messages(tmp_path, 2, text="SYNTHETIC_ONLY printf 'a  b'")
    store.messages.append({'thread_id': messages[1].thread_id, 'role': 'user', 'channel': 'tui',
        'content': "SYNTHETIC_ONLY printf 'a b'", 'now': 6003})
    payload = _collect(tmp_path, store).to_model_payload()
    assert len(payload['messages']) == 2
    assert {row['content_preview'] for row in payload['messages']} == {
        "SYNTHETIC_ONLY printf 'a  b'", "SYNTHETIC_ONLY printf 'a b'"}


def test_broadcast_preserves_each_frozen_file_source(tmp_path):
    from agent_py_agent.agent.memory_store.curator_inputs import _with_broadcast_identity

    store, _ = _store_messages(tmp_path, 2)
    batch = _collect(tmp_path, store)
    messages = tuple(replace(message, referenced_files=({'path': '/SYNTHETIC_ONLY_rule.md',
        'sha256': 'a' * 64, 'excerpt': 'SYNTHETIC_ONLY_SAME_RULE',
        'tool_call_id': f'SYNTHETIC_ONLY_CALL_{index}'},)) for index, message in enumerate(batch.messages))
    batch = replace(batch, messages=_with_broadcast_identity(messages))
    view = batch.to_model_payload()['messages']
    assert len(view) == 1
    assert {source['referenced_files'][0]['tool_call_id'] for source in view[0]['broadcast_sources']} == {
        'SYNTHETIC_ONLY_CALL_0', 'SYNTHETIC_ONLY_CALL_1'}
    changed = replace(messages[1], referenced_files=({**messages[1].referenced_files[0], 'sha256': 'b' * 64,
        'excerpt': 'SYNTHETIC_ONLY_CHANGED_RULE'},))
    batch = replace(batch, messages=_with_broadcast_identity((messages[0], changed)))
    assert len(batch.to_model_payload()['messages']) == 2


def _record_instruction(committer, daily, message):
    ref = {**message.ref(), 'quote': message.full_content}
    daily.append(DailyMemoryEvent('decision', 'SYNTHETIC_ONLY_RULE', 'user', origin='user_explicit',
        message_refs=(ref,), created_at='1970-01-01T01:40:00+00:00'))
    observation = CandidateObservation('user_preference', 'SYNTHETIC_ONLY_RULE', 'SYNTHETIC_ONLY_SUBJECT',
        MemoryScope('personal', 'personal'), 'user_explicit', source_message_refs=(ref,), evidence_refs=(ref,),
        observed_at='1970-01-01T01:40:00+00:00', promotion_target='user')
    _persist_candidate_observations(committer, [observation])


def test_broadcast_single_message_batches_are_still_one_observation(tmp_path):
    store, _ = _store_messages(tmp_path, 2)
    config = _config(batch_message_limit=1)
    first = _collect(tmp_path, store, config=config)
    assert len(first.messages) == 1
    second = collect_curator_inputs(message_source=CuratorMessageSource(store), audit_dir=tmp_path / 'audit',
        state=MemoryCuratorState(per_thread_cursors={first.messages[0].thread_id: first.messages[0].message_id}), config=config)
    assert len(second.messages) == 1 and first.messages[0].thread_id != second.messages[0].thread_id
    committer = object.__new__(CuratorBatchCommitter)
    committer.candidates = CandidateService(tmp_path / 'memory' / 'candidates.jsonl')
    daily = DailyMemoryStore(tmp_path / 'memory' / 'daily')
    for message in (*first.messages, *second.messages, *first.messages, *second.messages):
        _record_instruction(committer, daily, message)
    events = daily.list(day='1970-01-01')
    rows = committer.candidates.readonly_rows()
    assert len(events) == 1 and len(events[0].message_refs) == 2
    assert len(rows) == 1 and rows[0].occurrence_count == 1 and len(rows[0].source_message_refs) == 2


def test_same_thread_repetition_is_not_a_cross_thread_broadcast(tmp_path):
    store, original = _store_messages(tmp_path)
    store.messages.append({'thread_id': original[0].thread_id, 'role': 'user', 'channel': 'tui',
        'content': original[0].content, 'now': 6001})
    batch = _collect(tmp_path, store)
    assert len(batch.to_model_payload()['messages']) == 2
    committer = object.__new__(CuratorBatchCommitter)
    committer.candidates = CandidateService(tmp_path / 'memory' / 'candidates.jsonl')
    daily = DailyMemoryStore(tmp_path / 'memory' / 'daily')
    for message in batch.messages:
        _record_instruction(committer, daily, message)
    assert len(daily.list(day='1970-01-01')) == 2
    assert committer.candidates.readonly_rows()[0].occurrence_count == 2


def test_current_symlink_cannot_prove_historical_file_binding(tmp_path):
    alias = tmp_path / 'SYNTHETIC_ONLY_alias.md'
    target = tmp_path / 'SYNTHETIC_ONLY_target.md'
    target.write_text('SYNTHETIC_ONLY_CURRENT_TARGET')
    alias.symlink_to(target)
    store, messages = _store_messages(tmp_path, text=f'请读 `{alias}`。')
    source, _, _ = _artifact(tmp_path, messages[0], target)
    batch = _collect(tmp_path, store, tool_source=source)
    assert not batch.messages[0].referenced_files


def test_broadcast_metadata_still_respects_whole_input_budget(tmp_path):
    store, original = _store_messages(tmp_path, text='SYNTHETIC_ONLY_RULE_' + 'x' * 300)
    for index in range(10):
        store.messages.append({'thread_id': original[0].thread_id, 'role': 'user', 'channel': 'tui',
            'content': f'SYNTHETIC_ONLY_RULE_{index}_' + 'x' * 300, 'now': 6001 + index})
    batch = _collect(tmp_path, store, config=_config(max_input_chars=2000))
    assert batch.messages
    assert len(json.dumps(batch.to_model_payload(), ensure_ascii=False)) <= 2000


@pytest.mark.parametrize('mode', ['missing', 'oversized', 'blob', 'input_budget'])
def test_missing_or_oversized_file_archive_degrades_without_fabricating_evidence(tmp_path, mode):
    path = tmp_path / 'SYNTHETIC_ONLY_policy.md'
    store, messages = _store_messages(tmp_path, text=f'请读 `{path}`。')
    source, artifact, _ = _artifact(tmp_path, messages[0], path)
    payload = json.loads(artifact.read_text())
    if mode == 'missing':
        artifact.rename(artifact.with_suffix('.SYNTHETIC_ONLY_missing'))
    elif mode == 'oversized':
        payload['SYNTHETIC_ONLY_padding'] = 'x' * 1_048_576
        artifact.write_text(json.dumps(payload))
    elif mode == 'blob':
        payload.pop('content')
        payload['blob_v1'] = {'path': '/SYNTHETIC_ONLY_unverified'}
        artifact.write_text(json.dumps(payload))
    config = _config(max_input_chars=2000) if mode == 'input_budget' else _config()
    batch = _collect(tmp_path, store, tool_source=source, config=config)
    assert len(batch.messages) == 1 and batch.messages[0].message_id == messages[0].message_id
    assert not batch.messages[0].referenced_files
    assert not batch.load_errors


def test_rule_notice_tail_once(tmp_path, monkeypatch):
    from agent_py_agent.agent.agent_core.runtime import loop_support
    from agent_py_agent.agent.prompting_parts.memory_context import memory_context_text
    from agent_py_agent.tests.memory_confirmation_fixtures import confirm_candidate
    from agent_py_agent.tests.test_ctxopt_loop_support import _record
    from agent_py_agent.tests.test_memory_promotion_v2 import _explicit, _runtime

    service, conversations, thread = _runtime(tmp_path)
    owner = service.persona.owner_home
    frozen = [_record('SYNTHETIC_ONLY_FROZEN', content='SYNTHETIC_ONLY_SYSTEM_PREFIX')]
    monkeypatch.setattr(loop_support, '_tool_snapshots_for_run', lambda *_: (None, None))
    monkeypatch.setattr(loop_support, '_routed_memory_context_for_request', lambda *a, **kw: SimpleNamespace(injected_sections=[]))
    monkeypatch.setattr(loop_support, '_formal_memories_for_request', lambda *a, **kw: frozen)
    monkeypatch.setattr(loop_support, '_resume_context_for_request', lambda *a, **kw: (SimpleNamespace(injected=False), ''))
    monkeypatch.setattr(loop_support, 'build_runtime_main_context_bundle', lambda *a, **kw: None)
    agent = SimpleNamespace(config=SimpleNamespace(memory_top_k=2, memory_rule_notices_enabled=True,
        memory_rule_notices_max_chars=1200), memory=service.long_term, home_paths=SimpleNamespace(owner_home_dir=owner))
    request = SimpleNamespace(context_scope='default', user_prompt='SYNTHETIC_ONLY_REQUEST', task_id='',
        task_attributes={'conversation_thread_id': 'SYNTHETIC_ONLY_THREAD_B'}, inject=['SYNTHETIC_ONLY_EXISTING_TAIL'])
    before = loop_support._prepare_runtime_context(agent, request)
    prefix = memory_context_text(before.memories).encode()
    candidate = _explicit(service, conversations, thread, content='SYNTHETIC_ONLY_RULE_NOTICE',
        candidate_type='working_agreement', subject_key='SYNTHETIC_ONLY_RULE')
    assert confirm_candidate(service, candidate.candidate_id).promoted
    notice_path = owner / 'memory' / 'rule_notices.json'
    assert notice_path.exists()
    stored = json.loads(notice_path.read_text())['notices']
    assert len(stored) == 1 and stored[0]['version'] == 1
    after = loop_support._prepare_runtime_context(agent, request)
    again = loop_support._prepare_runtime_context(agent, request)
    assert memory_context_text(after.memories).encode() == prefix == memory_context_text(again.memories).encode()
    assert after.runtime_injections[:-1] == before.runtime_injections
    assert after.runtime_injections[-1].count('SYNTHETIC_ONLY_RULE_NOTICE') == 1
    assert again.runtime_injections == before.runtime_injections
    # 同一正式条目换版本必须再通知一次，重复回执不能生成新通知。
    entry_id = service.long_term.all()[0].entry_id
    update = _explicit(service, conversations, thread, content='SYNTHETIC_ONLY_UPDATED_RULE',
        candidate_type='working_agreement', subject_key='SYNTHETIC_ONLY_RULE',
        proposed_action='replace', target_entry_id=entry_id)
    assert confirm_candidate(service, update.candidate_id).promoted
    newer = loop_support._prepare_runtime_context(agent, request)
    assert 'SYNTHETIC_ONLY_UPDATED_RULE' in newer.runtime_injections[-1]
    assert 'SYNTHETIC_ONLY_RULE_NOTICE' not in newer.runtime_injections[-1]
    assert loop_support._prepare_runtime_context(agent, request).runtime_injections == before.runtime_injections


def test_rule_notice_disabled_and_isolated_do_not_consume(tmp_path, monkeypatch):
    from agent_py_agent.agent.agent_core.runtime import loop_support
    from agent_py_agent.tests.memory_confirmation_fixtures import confirm_candidate
    from agent_py_agent.tests.test_memory_promotion_v2 import _explicit, _runtime

    service, conversations, thread = _runtime(tmp_path)
    candidate = _explicit(service, conversations, thread, content='SYNTHETIC_ONLY_PENDING_RULE',
        candidate_type='working_agreement', subject_key='SYNTHETIC_ONLY_SCOPE_RULE')
    assert confirm_candidate(service, candidate.candidate_id).promoted
    path = service.persona.owner_home / 'memory' / 'rule_notices.json'
    assert path.exists()
    # 只接管无关召回/恢复依赖，通知消费本身走生产实现。
    monkeypatch.setattr(loop_support, '_tool_snapshots_for_run', lambda *_: (None, None))
    monkeypatch.setattr(loop_support, '_routed_memory_context_for_request', lambda *a, **kw: SimpleNamespace(injected_sections=[]))
    monkeypatch.setattr(loop_support, '_formal_memories_for_request', lambda *a, **kw: [])
    monkeypatch.setattr(loop_support, '_resume_context_for_request', lambda *a, **kw: (SimpleNamespace(injected=False), ''))
    monkeypatch.setattr(loop_support, 'build_runtime_main_context_bundle', lambda *a, **kw: None)
    agent = SimpleNamespace(config=SimpleNamespace(memory_top_k=1, memory_rule_notices_enabled=False),
        memory=service.long_term, home_paths=SimpleNamespace(owner_home_dir=service.persona.owner_home))
    request = SimpleNamespace(context_scope='default', user_prompt='SYNTHETIC_ONLY_INPUT', task_id='',
        task_attributes={'conversation_thread_id': 'SYNTHETIC_ONLY_THREAD_B'}, inject=[])
    assert not loop_support._prepare_runtime_context(agent, request).runtime_injections
    agent.config.memory_rule_notices_enabled = True
    request.context_scope = 'task_local'
    assert not loop_support._prepare_runtime_context(agent, request).runtime_injections
    request.context_scope = 'default'
    assert 'SYNTHETIC_ONLY_PENDING_RULE' in loop_support._prepare_runtime_context(agent, request).runtime_injections[-1]


def test_user_notice_uses_formal_entry_and_late_callback_cannot_revive_old_rule(tmp_path):
    from agent_py_agent.agent.memory_store.rule_notices import (
        RuleNoticeStore,
        publish_promoted_rule,
    )
    from agent_py_agent.tests.test_memory_promotion_v2 import _runtime

    service, _, _ = _runtime(tmp_path)
    persona = service.persona
    path = persona.path_for('user')
    persona.versions_path.parent.mkdir(parents=True, exist_ok=True)
    old, new = 'SYNTHETIC_ONLY_OLD_RULE', 'SYNTHETIC_ONLY_NEW_RULE'
    ledger = []

    def formal_snapshot(text, version):
        # 隔离夹具构造已提交文档/版本；生产通知仍使用原仓库读取，不声称验证了正式写入事务。
        raw = '# USER\n\n- ' + text + '\n'
        path.write_text(raw)
        ledger.append({'target': 'user', 'sha256': hashlib.sha256(raw.encode()).hexdigest(), 'version': version})
        persona.versions_path.write_text(''.join(json.dumps(row) + '\n' for row in ledger))
        snapshot = persona.list_entries('user')
        assert snapshot['version'] == version
        return snapshot['entries'][0]['entry_id']

    first_id = formal_snapshot(old, 1)
    candidate = SimpleNamespace(candidate_type='user_preference', content=old, proposed_action='add',
        target_entry_id='', source_message_refs=(), scope={'scope_type': 'personal', 'scope_key': 'personal'})
    publish_promoted_rule(service, candidate, f'USER.md#{first_id}')
    new_id = formal_snapshot(new, 2)
    update = SimpleNamespace(**{**vars(candidate), 'content': new, 'proposed_action': 'replace', 'target_entry_id': first_id})
    publish_promoted_rule(service, update, f'USER.md#{new_id}')
    # 旧晋升的收尾晚到，也不能把旧条目/旧正文挂到新文档版本上。
    assert publish_promoted_rule(service, candidate, f'USER.md#{first_id}') is None
    store = RuleNoticeStore(persona.owner_home)
    [notice] = store._read(store.path)['notices']
    assert notice['text'] == new and notice['version'] == 2
    assert store.take('SYNTHETIC_ONLY_READER') == new


def test_hot_notice_reads_exact_formal_rule_not_delayed_candidate_body(tmp_path):
    from agent_py_agent.agent.memory_store.lessons import (
        HOT_SCHEMA_VERSION,
        HotRuleRecord,
        _render_hot,
    )
    from agent_py_agent.agent.memory_store.rule_notices import (
        RuleNoticeStore,
        publish_promoted_rule,
    )
    from agent_py_agent.tests.test_memory_promotion_v2 import _runtime

    service, _, _ = _runtime(tmp_path)
    rule = HotRuleRecord(HOT_SCHEMA_VERSION, 'SYNTHETIC_ONLY_HOT', 'SYNTHETIC_ONLY_CANDIDATE',
        'SYNTHETIC_ONLY_LESSON', 'SYNTHETIC_ONLY_LESSON_REF', 'SYNTHETIC_ONLY_FORMAL_RULE',
        3, ('SYNTHETIC_ONLY_EVIDENCE',), '2026-11-02T00:00:00+00:00')
    service.hot.path.write_text(_render_hot([rule]))
    candidate = SimpleNamespace(candidate_type='hot_rule', content='SYNTHETIC_ONLY_STALE_BODY',
        proposed_action='add', target_entry_id='', source_message_refs=(), scope={})
    reference = 'memory-hot.md#SYNTHETIC_ONLY_HOT'
    notice = publish_promoted_rule(service, candidate, reference)
    assert notice['text'] == rule.rule
    service.hot.path.write_text(_render_hot([]))
    assert publish_promoted_rule(service, candidate, reference) is None
    # 已发布提醒不是正式权威；这个反例只核验晚到收尾不会新增一条已离开HOT的规则。
    store = RuleNoticeStore(service.persona.owner_home)
    assert len(store._read(store.path)['notices']) == 1
