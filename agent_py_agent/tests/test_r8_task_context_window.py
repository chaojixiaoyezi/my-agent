"""热上下文只读取有界近期和结构化必需任务；完整历史导出仍独立保留。"""
from __future__ import annotations

import json
from dataclasses import replace

import pytest

from agent_py_agent.agent.conversation import ConversationStore, store_tasks, task_history
from agent_py_agent.agent.conversation.models import ThreadTaskLink


def _history(tmp_path, count):
    store = ConversationStore(tmp_path / 'conversations')
    thread = store.threads.get_or_create({'canonical_user_id': 'isolated', 'channel': 'internal'})
    ids = tuple(f'task-{number}' for number in range(count))
    for number, identifier in enumerate(ids):
        link = ThreadTaskLink(thread.thread_id, identifier, '历史任务', status='active' if number == 0 else 'completed')
        store.storage.task_path(identifier).write_text(json.dumps(link.to_dict()))
    task_history.initialize(store.storage.thread_path(thread.thread_id), replace(thread, task_ids=ids))
    thread = replace(thread, task_ids=ids[-128:], active_task_ids=(ids[0],), workspace_task_id=ids[1], task_history_version=1)
    store.threads.write(thread)
    return store, thread, ids


@pytest.mark.parametrize('count', [140, 1000])
def test_model_context_task_link_reads_do_not_grow_with_completed_history(tmp_path, monkeypatch, count):
    store, thread, ids = _history(tmp_path, count)
    original, reads = store_tasks._read_task_link, []

    def read(path, identifier):
        reads.append(identifier)
        return original(path, identifier)

    monkeypatch.setattr(store_tasks, '_read_task_link', read)
    for current in (store, ConversationStore(store.storage.root, initialize=False)):
        reads.clear()
        bundle, errors = current.context_bundle_report(thread.thread_id, recent_limit=20)
        print(json.dumps({'history': count, 'context_link_reads': len(reads)}))
        assert len(reads) <= 22
        assert not errors and {row['task_id'] for row in bundle['tasks']} == {*ids[-20:], ids[0], ids[1]}
        assert bundle['task_coverage']['status'] == 'window' and bundle['task_coverage']['complete'] is False
    reads.clear()
    links, errors = store.tasks.list_report(thread.thread_id)
    assert not errors and tuple(row.task_id for row in links) == ids
    assert len(reads) == count


def test_required_old_terminal_task_is_selected_without_loading_other_history(tmp_path):
    store, thread, ids = _history(tmp_path, 140)
    links, errors, warnings, coverage = store.tasks.context_report(
        thread.thread_id, recent_limit=3, required_task_ids=(ids[2],))
    assert not errors and not warnings
    assert {row.task_id for row in links} == {*ids[-3:], *ids[:3]}
    assert coverage['history_complete'] is False


@pytest.mark.parametrize('selection', ['recent', 'active', 'workspace', 'required'])
def test_every_selected_task_must_belong_to_requested_thread(tmp_path, selection):
    store, thread, ids = _history(tmp_path, 140)
    selected = {'recent': ids[-1], 'active': ids[0], 'workspace': ids[1], 'required': ids[2]}[selection]
    path = store.storage.task_path(selected)
    payload = json.loads(path.read_text())
    payload['thread_id'] = 'foreign-thread'
    path.write_text(json.dumps(payload))
    bundle, errors = store.context_bundle_report(thread.thread_id, recent_limit=3, required_task_ids=(ids[2],))
    assert selected not in {row['task_id'] for row in bundle['tasks']}
    assert any(row.get('task_id') == selected and row.get('scope_resolution') == 'thread_mismatch' for row in errors)
    assert bundle['task_coverage']['status'] == 'unknown'


@pytest.mark.parametrize('damage', ['missing', 'unknown', 'incomplete'])
def test_context_does_not_guess_or_repair_unproven_history(tmp_path, damage):
    store, thread, _ids = _history(tmp_path, 140)
    path = task_history.history_root(store.storage.thread_path(thread.thread_id)) / 'index.json'
    if damage == 'missing':
        path.unlink()
    else:
        data = json.loads(path.read_text())
        data['version' if damage == 'unknown' else 'incomplete'] = 99 if damage == 'unknown' else True
        path.write_text(json.dumps(data))
    before = path.read_bytes() if path.exists() else None
    bundle, errors = store.context_bundle_report(thread.thread_id)
    assert errors and bundle['tasks'] == []
    assert bundle['task_coverage']['status'] == 'unknown'
    assert (path.read_bytes() if path.exists() else None) == before


def test_background_keeps_explicit_old_task_and_reads_lineage_once(tmp_path):
    from types import SimpleNamespace

    from agent_py_agent.agent.conversation.background_context import (
        BackgroundContextLoad,
        load_context_bundle,
    )

    store, thread, ids = _history(tmp_path, 140)
    calls = []
    manager = SimpleNamespace(
        load=lambda _identifier: SimpleNamespace(root_id=ids[2]),
        list_runs_for_root_report=lambda identifier: (
            calls.append(identifier) or SimpleNamespace(load_errors=[], runs=[SimpleNamespace(id=ids[3], root_id=ids[2])])),
    )
    state = BackgroundContextLoad(SimpleNamespace(subagents=manager), store, thread, ids[2], None, None, [])
    bundle = load_context_bundle(state)
    assert not state.load_errors
    assert {row['task_id'] for row in bundle['tasks']} == {ids[2], ids[3]}
    assert calls == [ids[2]]
    assert bundle['task_coverage']['selection'] == 'explicit_task_scope'
    assert 'history_positions' not in bundle['task_coverage']


def test_coverage_distinguishes_complete_history_from_zero_window(tmp_path):
    store, thread, ids = _history(tmp_path, 3)
    links, errors, warnings, coverage = store.tasks.context_report(thread.thread_id)
    assert {row.task_id for row in links} == set(ids) and not errors and not warnings
    assert coverage['status'] == 'complete' and coverage['history_complete'] is True
    links, errors, warnings, coverage = store.tasks.context_report(thread.thread_id, recent_limit=0)
    assert {row.task_id for row in links} == {ids[0], ids[1]} and not errors and not warnings
    assert coverage['status'] == 'window' and coverage['history_complete'] is False


@pytest.mark.parametrize('conflict', [False, True])
def test_named_bind_checks_only_active_members_after_long_terminal_history(tmp_path, monkeypatch, conflict):
    store, thread, ids = _history(tmp_path, 1000)
    path = store.storage.task_path(ids[0])
    data = json.loads(path.read_text())
    data.update(work_kind='audit', work_name='same' if conflict else 'other')
    path.write_text(json.dumps(data))
    monkeypatch.setattr(store.tasks, 'list_report', lambda *_args, **_kwargs: pytest.fail('full history read'))
    request = {'thread_id': thread.thread_id, 'task_id': 'new-named', 'work_kind': 'audit', 'work_name': 'same'}
    if conflict:
        with pytest.raises(ValueError, match='already exists'):
            store.tasks.bind(request)
        assert not store.storage.task_path('new-named').exists()
    else:
        assert store.tasks.bind(request).task_id == 'new-named'


@pytest.mark.parametrize('narrow', [False, True])
def test_task_coverage_survives_real_budget_and_rendering(tmp_path, narrow):
    from agent_py_agent.agent.conversation.background_context import (
        PreparedBackgroundContext,
        render_background_context,
    )
    from agent_py_agent.agent.conversation.context_budget import (
        BackgroundContextBudget,
        BackgroundContextPayloadRequest,
    )

    store, thread, ids = _history(tmp_path, 140)
    bundle, errors = store.context_bundle_report(thread.thread_id, required_task_ids=(ids[2],))
    assert not errors and bundle['tasks'][0]['task_id'] == ids[2]
    payload = BackgroundContextPayloadRequest(bundle, None, [], {}, budget=BackgroundContextBudget(max_total_tokens=1))
    prepared = PreparedBackgroundContext(payload, (), {}, (), ids[2], narrow, False)
    rendered = render_background_context(prepared)
    assert 'Task Coverage' in rendered
    assert '"history_complete": false' in rendered and '"status": "window"' in rendered


def test_named_bind_keeps_active_read_error_blocking(tmp_path):
    from agent_py_agent.agent.runtime_errors import DataCorruptionError

    store, thread, ids = _history(tmp_path, 140)
    path = store.storage.task_path(ids[0])
    path.write_bytes(b'{broken')
    with pytest.raises(DataCorruptionError, match='unavailable'):
        store.tasks.bind({'thread_id': thread.thread_id, 'task_id': 'new-named', 'work_kind': 'audit', 'work_name': 'same'})
    assert path.read_bytes() == b'{broken' and not store.storage.task_path('new-named').exists()
