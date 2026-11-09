"""长期线程的Gateway上下文读取量只随窗口、活动集和准确引用增长。"""
from __future__ import annotations

import json
from dataclasses import replace
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.conversation import ConversationStore, store_tasks, task_promotion
from agent_py_agent.agent.conversation.models import ThreadTaskLink
from agent_py_agent.agent.gateway_parts import request_context, request_history


def _history(tmp_path, count):
    store = ConversationStore(tmp_path / 'conversations')
    workspace = tmp_path / 'workspace'
    workspace.mkdir()
    thread = store.threads.get_or_create({'canonical_user_id': 'local', 'channel': 'internal',
                                          'owner_home': str(tmp_path)})
    ids = tuple(f'old-{number}' for number in range(count))
    for number, identifier in enumerate(ids):
        link = ThreadTaskLink(thread.thread_id, identifier, '历史工作',
                              status='active' if number == 0 else 'completed',
                              created_at=float(number), task_path=str(workspace))
        store.storage.task_path(identifier).write_text(json.dumps(link.to_dict()))
    thread = replace(thread, task_ids=ids, active_task_ids=(ids[0],), workspace_task_id=ids[1])
    store.storage.thread_path(thread.thread_id).write_text(json.dumps(thread.to_dict()))
    store.tasks.bind({'thread_id': thread.thread_id, 'task_id': 'current', 'status': 'completed',
                      'task_path': str(workspace)})
    # 只用冷构造实例量测，迁移与建夹具成本不混入稳态读取。
    cold = ConversationStore(store.storage.root)
    return cold, cold.threads.require(thread.thread_id), workspace


def _read_counter(monkeypatch):
    original = store_tasks._read_task_link
    reads = []

    def read(path, task_id, **kwargs):
        reads.append(task_id)
        return original(path, task_id, **kwargs)

    monkeypatch.setattr(store_tasks, '_read_task_link', read)
    return reads


@pytest.mark.parametrize('count', [140, 1000])
def test_gateway_context_task_links_are_bounded_after_restart(tmp_path, monkeypatch, count):
    store, thread, _ = _history(tmp_path, count)
    reads = _read_counter(monkeypatch)
    errors = request_context._GatewayLoadErrors()
    links, failures = request_context._gateway_task_links(store, thread.thread_id, errors)
    assert not failures
    assert {'old-0', 'old-1', 'current'} <= {link.task_id for link in links}
    assert len(reads) <= 130
    assert len(links) <= 130
    assert any(row['code'] == 'task_context_window' for row in errors.warnings)


def test_gateway_bound_old_task_outside_window_remains_selectable(tmp_path, monkeypatch):
    store, thread, _ = _history(tmp_path, 160)
    reads = _read_counter(monkeypatch)
    errors = request_context._GatewayLoadErrors()
    selected = request_context._gateway_workspace_task(
        store, thread, None, errors, request_id='request',
        request={'id': 'request', 'conversation_runtime': {'request_id': 'request',
                 'thread_id': thread.thread_id, 'task_id': 'old-3'}},
    )
    assert not errors
    assert selected is not None and selected.task_id == 'old-3'
    assert len(reads) <= 132


def test_gateway_redactions_do_not_enumerate_terminal_history(tmp_path, monkeypatch):
    store, thread, workspace = _history(tmp_path, 160)
    reads = _read_counter(monkeypatch)
    context = request_context.GatewayAskRunContext(
        agent=SimpleNamespace(conversation_store=store), request={'task_id': 'old-3'},
        request_path=tmp_path / 'request', response_path=tmp_path / 'response',
        request_id='request', on_chunk=None,
    )
    conversation = request_context.GatewayConversationContext(
        thread_id=thread.thread_id, workspace_task=request_context.GatewayWorkspaceSelection(
            task_id='old-3', status='completed', goal='原工作', task_path=str(workspace)),
    )
    pairs = request_history.gateway_identifier_redactions(context, conversation)
    assert ('old-3', '当前任务') in pairs
    assert ('old-0', '当前任务') in pairs
    assert len(reads) <= 131


def _completion(store, thread_id, number, *, root='old-3', handled=False):
    return store.observations.append({'thread_id': thread_id, 'event_type': 'subagent_runner_finished',
        'source_agent_id': f'child-{number}', 'parent_agent_id': root, 'root_task_id': root,
        'requires_main_agent': True, 'handled_at': 1.0 if handled else 0.0,
        'now': float(number + 1), 'metadata': {'task_id': f'child-{number}', 'status': 'DONE',
        'completion_schema_version': 'subagent-completion.v1', 'completion_message': '已完成'}})


def test_recent_completion_keeps_old_root_without_full_history(tmp_path, monkeypatch):
    store, thread, workspace = _history(tmp_path, 160)
    _completion(store, thread.thread_id, 1)
    reads = _read_counter(monkeypatch)
    original = store.observations.recent_report

    def no_unbounded(*args, **kwargs):
        assert kwargs.get('limit', 20) > 0, '普通请求不能读取完整观察历史'
        return original(*args, **kwargs)

    monkeypatch.setattr(store.observations, 'recent_report', no_unbounded)
    selected = request_context.GatewayWorkspaceSelection(
        task_id='current', status='active', goal='继续', task_path=str(workspace))
    errors = request_context._GatewayLoadErrors()
    context = request_context._gateway_subagent_completion_context(store, thread.thread_id, selected, errors)
    assert not errors
    assert context['completion_root_task_ids'] == ['old-3']
    assert context['items'][0]['task_id'] == 'child-1'
    assert len(reads) <= 132
    assert context['coverage']['source_exhausted'] is True


def test_completion_window_discloses_unknown_older_total(tmp_path):
    store, thread, workspace = _history(tmp_path, 160)
    for number in range(160):
        _completion(store, thread.thread_id, number, handled=True)
    selected = request_context.GatewayWorkspaceSelection(
        task_id='current', status='active', goal='继续', task_path=str(workspace))
    context = request_context._gateway_subagent_completion_context(store, thread.thread_id, selected, [])
    assert context['total'] <= 128
    assert context['items'][-1]['task_id'] == 'child-159'
    assert context['coverage']['source_exhausted'] is False
    assert context['coverage']['history_coverage'] == 'unknown'


def test_exact_workspace_successor_reads_only_its_chain(tmp_path, monkeypatch):
    store, thread, _ = _history(tmp_path, 160)
    old = store.tasks.load('old-3')
    store.storage.task_path('old-3').write_text(json.dumps(
        replace(old, status='superseded', superseded_by_task_id='old-4').to_dict()))
    reads = _read_counter(monkeypatch)
    link = task_promotion._reusable_conversation_workspace_link(store, thread.thread_id, 'old-3')
    assert link is not None and link.task_id == 'old-4'
    assert reads == ['old-3', 'old-4']


def test_successor_allocation_does_not_load_other_terminal_links(tmp_path, monkeypatch):
    store, thread, workspace = _history(tmp_path, 160)
    reads = _read_counter(monkeypatch)
    identifier = task_promotion._terminal_successor_task_id(store, thread.thread_id,
        base_id='old-3', source_id='source', task_path=str(workspace))
    assert identifier.startswith('old-3-continue-')
    assert len(reads) <= 6


def test_completion_ids_actually_sent_to_model_are_redacted(tmp_path):
    store, thread, workspace = _history(tmp_path, 160)
    context = request_context.GatewayAskRunContext(
        agent=SimpleNamespace(conversation_store=store), request={},
        request_path=tmp_path / 'request', response_path=tmp_path / 'response',
        request_id='request', on_chunk=None)
    conversation = request_context.GatewayConversationContext(
        thread_id=thread.thread_id,
        workspace_task=request_context.GatewayWorkspaceSelection(
            task_id='current', status='active', goal='继续', task_path=str(workspace)),
        subagent_completions={'completion_root_task_ids': ['old-3'],
                             'items': [{'task_id': 'child-1', 'root_task_id': 'old-3'}]})
    pairs = request_history.gateway_identifier_redactions(context, conversation)
    assert ('old-3', '当前任务') in pairs
    assert ('child-1', '子代理任务') in pairs


def test_empty_recent_completion_window_keeps_unknown_coverage(tmp_path):
    store, thread, workspace = _history(tmp_path, 160)
    for number in range(160):
        store.observations.append({'thread_id': thread.thread_id,
                                   'event_type': 'ordinary_status', 'now': float(number + 1)})
    selected = request_context.GatewayWorkspaceSelection(
        task_id='current', status='active', goal='继续', task_path=str(workspace))
    context = request_context._gateway_subagent_completion_context(store, thread.thread_id, selected, [])
    assert context['items'] == []
    assert context['coverage']['history_coverage'] == 'unknown'


def test_unhandled_completion_survives_many_newer_ordinary_events(tmp_path):
    store, thread, workspace = _history(tmp_path, 160)
    _completion(store, thread.thread_id, 1)
    for number in range(300):
        store.observations.append({'thread_id': thread.thread_id,
            'event_type': 'ordinary_status', 'now': float(number + 10), 'handled_at': 1.0})
    # 只推进真实投影的固定片；迁移追平后，当前义务不能因物理尾窗移动而消失。
    for _ in range(20):
        if store.observations._index.maintenance()['coverage'] == 'complete':
            break
    else:
        pytest.fail('观察投影未持续推进到完整')
    selected = request_context.GatewayWorkspaceSelection(
        task_id='current', status='active', goal='继续', task_path=str(workspace))
    errors = request_context._GatewayLoadErrors()
    context = request_context._gateway_subagent_completion_context(store, thread.thread_id, selected, errors)
    assert not errors
    assert context['items'][0]['task_id'] == 'child-1'
    assert context['coverage']['pending_coverage'] == 'complete'
    assert context['coverage']['history_coverage'] == 'unknown'
