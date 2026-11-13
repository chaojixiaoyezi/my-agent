"""子代理公开历史只读原线程；分页重连、权限与发送方来源沿原合同。"""
import json
from dataclasses import replace

import pytest

from agent_py_agent.agent.conversation.agent_thread_store import ensure_agent_thread_record
from agent_py_agent.agent.conversation.business_access import BusinessActor, BusinessError
from agent_py_agent.tests.test_business_v12_f7_observation import fixture


def setup_child(tmp_path):
    case = fixture(tmp_path)
    run_id = 'child-run'
    root = tmp_path / 'runs' / run_id
    root.mkdir(parents=True)
    state = root / 'canonical_state.json'
    state.write_text(json.dumps({'id': run_id, 'owner': case.owner, 'agent_thread_id': 'child-thread'}))
    (root / 'run.json').write_text(json.dumps({'canonical_state_ref': str(state)}))
    case.store.tasks.bind({'thread_id': 'background', 'task_id': run_id, 'task_path': str(root), 'goal': '公开子任务'})
    thread = ensure_agent_thread_record(case.store.threads, {'thread_id': 'child-thread', 'agent_run_id': run_id,
        'owner_id': case.owner, 'owner_home': str(tmp_path)})
    case.child_thread = thread
    case.params = {'target': {'kind': 'subagent', 'id': run_id}, 'limit': 1}
    return case


def append(case, content, *, role='assistant', metadata=None, thread_id='child-thread'):
    return case.store.messages.append({'thread_id': thread_id, 'role': role, 'content': content,
        'metadata': {'agent_run_id': 'child-run', 'agent_attempt_id': case.child['attempt_id'], **(metadata or {})}})


def test_child_history_bounded_pagination_reconnect_and_no_parent_or_private_data(tmp_path):
    case = setup_child(tmp_path)
    append(case, 'PARENT_SECRET', thread_id='background')
    first = append(case, '公开输入', role='user')
    second = append(case, '公开回答', metadata={'canonical_native_messages': {'thinking': 'PRIVATE_THINKING'}})
    one = case.service.query('messages.list', case.params, actor=case.actor)
    assert one['items'][0]['thread_id'] == 'child-thread'
    assert one['coverage'] == 'partial' and one['next_cursor']
    # 断线重连读取同一冻结页；追加新消息不改变已签游标的目标。
    append(case, '后续回答')
    two = case.service.query('messages.list', {**case.params, 'cursor': one['next_cursor']}, actor=case.actor)
    replay = case.service.query('messages.list', {**case.params, 'cursor': one['next_cursor']}, actor=case.actor)
    assert replay == two
    assert {one['items'][0]['message_id'], two['items'][0]['message_id']} == {first.message_id, second.message_id}
    assert two['coverage'] == 'complete'
    assert 'PARENT_SECRET' not in repr(one) + repr(two)
    assert 'PRIVATE_THINKING' not in repr(one) + repr(two)


def test_child_missing_or_wrong_owner_state_does_not_fall_back_to_parent(tmp_path):
    case = setup_child(tmp_path)
    append(case, 'PARENT_SECRET', thread_id='background')
    state = tmp_path / 'runs/child-run/canonical_state.json'
    state.unlink()
    missing = case.service.query('messages.list', case.params, actor=case.actor)
    assert missing['coverage'] == 'unknown' and missing['items'] == []
    state.write_text(json.dumps({'id': 'child-run', 'owner': 'other/owner', 'agent_thread_id': 'background'}))
    with pytest.raises(BusinessError, match='ACCESS_DENIED'):
        case.service.query('messages.list', case.params, actor=case.actor)


def test_child_actor_space_denial_and_attempt_or_file_replacement_expires_cursor(tmp_path):
    case = setup_child(tmp_path)
    append(case, 'one')
    append(case, 'two')
    denied = BusinessActor(case.owner, 'user', case.service, lambda _action, facts: facts.get('space_id') != 'space-a')
    with pytest.raises(BusinessError, match='ACCESS_DENIED'):
        case.service.query('messages.list', case.params, actor=denied)
    page = case.service.query('messages.list', case.params, actor=case.actor)
    path = case.store.storage.message_path('child-thread')
    old = path.with_suffix('.old')
    path.rename(old)
    path.write_bytes(old.read_bytes())
    with pytest.raises(BusinessError, match='CURSOR_EXPIRED'):
        case.service.query('messages.list', {**case.params, 'cursor': page['next_cursor']}, actor=case.actor)


def test_sender_origin_requires_visible_same_owner_thread_and_keeps_message_id(tmp_path):
    case = setup_child(tmp_path)
    first = append(case, '后台补充', role='host', metadata={'origin_kind': 'session_message',
        'origin_thread_id': 'background', 'input_kind': 'guidance'})
    params = {**case.params, 'limit': 10}
    page = case.service.query('messages.list', params, actor=case.actor)
    assert page['items'][0]['message_id'] == first.message_id
    assert page['items'][0]['source'] == {'kind': 'session_message', 'source_ref': 'background', 'revision': None}
    restricted = BusinessActor(case.owner, 'user', case.service,
        lambda _action, facts: facts.get('thread_id') != 'controller')
    second = append(case, '另一个来源', role='host', metadata={'origin_kind': 'session_message',
        'origin_thread_id': 'controller'})
    projected = case.service.query('messages.list', params, actor=restricted)
    value = next(row for row in projected['items'] if row['message_id'] == second.message_id)
    assert value['source']['source_ref'] == second.message_id
    assert value['source']['kind'] == 'canonical_message'
    # 同名规范线程被移给其它owner也不得泄漏sender引用。
    case.store.threads.update_atomic('controller', lambda thread: replace(thread, owner_id='foreign/owner'))
    projected = case.service.query('messages.list', params, actor=case.actor)
    value = next(row for row in projected['items'] if row['message_id'] == second.message_id)
    assert value['source']['source_ref'] == second.message_id


def test_canonical_background_message_keeps_sender_scope_private_when_denied(tmp_path):
    from agent_py_agent.agent.conversation.background_team_lifecycle import BACKGROUND_IDENTITY_KEY
    from agent_py_agent.agent.conversation.models import ConversationThread
    from agent_py_agent.agent.conversation.store_threads import create_thread_atomic

    case = fixture(tmp_path)
    create_thread_atomic(case.store.threads, ConversationThread(thread_id='other-space', owner_id=case.owner,
        canonical_user_id=case.owner, metadata={BACKGROUND_IDENTITY_KEY: {'agent_id': 'other-space',
            'managed_by_thread_id': 'controller', 'space_id': 'space-b', 'project_ref': None,
            'identity_state': 'available', 'after_task': 'reuse', 'identity_revision': 1}}))
    row = case.store.messages.append({'thread_id': 'background', 'role': 'host', 'content': '受限来源输入',
        'metadata': {'origin_kind': 'session_message', 'origin_thread_id': 'other-space', 'input_kind': 'guidance'}})
    actor = BusinessActor(case.owner, 'user', case.service, lambda _action, facts: facts.get('space_id') != 'space-b')
    page = case.service.query('messages.list', {'target': {'kind': 'background_main', 'id': 'background'}}, actor=actor)
    assert page['items'][0]['message_id'] == row.message_id
    assert page['items'][0]['source']['source_ref'] == row.message_id
    assert 'other-space' not in repr(page)


def test_new_attempt_invalidates_child_history_cursor(tmp_path):
    case = setup_child(tmp_path)
    append(case, 'one')
    append(case, 'two')
    first = case.service.query('messages.list', case.params, actor=case.actor)
    case.runtime.create_attempt(case.child['agent_run_id'])
    with pytest.raises(BusinessError, match='CURSOR_EXPIRED'):
        case.service.query('messages.list', {**case.params, 'cursor': first['next_cursor']}, actor=case.actor)


def test_child_history_filtered_row_advances_and_corrupt_source_is_explicit(tmp_path):
    case = setup_child(tmp_path)
    append(case, '公开一')
    append(case, 'PRIVATE_OTHER_RUN', metadata={'agent_run_id': 'another-run'})
    append(case, '公开二')
    first = case.service.query('messages.list', {**case.params, 'limit': 2}, actor=case.actor)
    assert first['next_cursor'] and 'PRIVATE_OTHER_RUN' not in repr(first)
    second = case.service.query('messages.list', {**case.params, 'cursor': first['next_cursor']}, actor=case.actor)
    assert second['items'] and second['next_cursor'] is None
    case.store.storage.message_path('child-thread').write_bytes(b'{invalid json\n')
    bad = case.service.query('messages.list', case.params, actor=case.actor)
    assert bad['coverage'] == 'unknown'
    assert bad['history_boundary']['reason_code'] == 'SUBAGENT_TRANSCRIPT_UNREADABLE'
