"""终结且无业务绑定的内部事件不能永久挡住后续真实事件；未知仍等待。"""
from __future__ import annotations

import json

import pytest

from agent_py_agent.tests.test_business_v12_f7_observation import close_observed_task_run, fixture
from agent_py_agent.tests.test_mega_business_api_actions import background_http as background_http


def position(case, cursor, filters=None):
    scope = case.service.scope_digest('events.list', {'filter': filters or {}}, case.actor)
    return case.service.cursor_position(cursor, scope)['after']


@pytest.mark.parametrize('filter_space', [False, True])
def test_settled_unbound_prefix_advances_in_bounded_pages_without_exposure(tmp_path, filter_space):
    case = fixture(tmp_path)
    filters = {'space_id': 'space-a'} if filter_space else {}
    baseline = case.service.query('events.list', {'filter': filters}, actor=case.actor)
    internal = case.runtime.record_run_creation(owner_id=case.owner, run_id='internal-without-thread', thread_id='')
    close_observed_task_run(case.runtime, [internal])
    target = case.runtime.append_event(event_type='attempt.progress', agent_run_id=case.root['agent_run_id'],
        attempt_id=case.root['attempt_id'], task_run_id=case.root['task_run_id'], payload={'private': 'do-not-expose'})
    cursor = baseline['next_cursor']
    previous = position(case, cursor, filters)
    events = []
    for _ in range(8):
        page = case.service.query('events.list', {'filter': filters, 'limit': 1, 'cursor': cursor}, actor=case.actor)
        events.extend(page['events'])
        cursor = page['next_cursor']
        current = position(case, cursor, filters)
        if target['event_id'] in {event['event_id'] for event in events}:
            break
        assert page['coverage'] == 'partial' and current > previous
        previous = current
    assert [event['event_id'] for event in events] == [target['event_id']]
    assert 'internal-without-thread' not in json.dumps(events)
    assert 'do-not-expose' not in json.dumps(events)
    assert case.store.session_tasks.require(case.task.task_id).status == 'queued'


@pytest.mark.parametrize('binding', ['active', 'missing_session_task'])
def test_unresolved_or_active_unbound_record_keeps_cursor_for_recovery(tmp_path, binding):
    case = fixture(tmp_path)
    baseline = case.service.query('events.list', {'filter': {}}, actor=case.actor)
    metadata = {} if binding == 'active' else {'session_task_ref': 'missing-task'}
    internal = case.runtime.record_run_creation(owner_id=case.owner, run_id='unbound-pending', thread_id='',
                                                task_run_metadata=metadata)
    if binding != 'active':
        close_observed_task_run(case.runtime, [internal])
    cursor = baseline['next_cursor']
    page = case.service.query('events.list', {'filter': {}, 'limit': 1, 'cursor': cursor}, actor=case.actor)
    assert page['events'] == [] and page['coverage'] == 'partial'
    assert position(case, page['next_cursor']) == position(case, cursor)


def test_native_nonempty_event_cursor_reconnect_and_owner_isolation(background_http):
    from agent_py_agent.agent.runtime_db.repository import RuntimeRepository
    from agent_py_agent.agent.runtime_db.schema import runtime_db_path
    from agent_py_agent.tests.test_mega_business_api_actions import (
        background_call,
        observed_native_task,
        open_native_observation,
    )

    services, task = observed_native_task(background_http)
    runtime = RuntimeRepository(runtime_db_path(services.owner_home))
    internal = runtime.record_run_creation(owner_id=services.owner_id, run_id='internal-unbound', thread_id='')
    close_observed_task_run(runtime, [internal])
    visible = runtime.record_run_creation(owner_id=services.owner_id, run_id='real-visible',
        thread_id=task.target_thread_id, task_run_metadata={
            'business_context': task.metadata['business_context'], 'session_task_ref': task.task_id})
    foreign = runtime.record_run_creation(owner_id='other-owner', run_id='foreign-hidden', thread_id='foreign-thread')
    close_observed_task_run(runtime, [foreign])
    with runtime._runtime_connection() as conn:
        expected = [row[0] for row in conn.execute('SELECT event_id FROM runtime_events WHERE task_run_id=? ORDER BY seq',
                                                  (visible['task_run_id'],))]
    assert len(expected) == 2
    received = []
    cursor = None
    for _ in range(10):
        params = {'filter': {}, 'limit': 1}
        if cursor:
            params['cursor'] = cursor
        status, reply = background_call(background_http, 'events.list', params)
        assert status == 200, reply
        page = reply['data']
        received.extend(row['event_id'] for row in page['events'])
        if cursor == page['next_cursor']:
            break
        cursor = page['next_cursor']
    assert received == expected
    connection, response = open_native_observation(background_http, {'request_id':'nonempty-stream','filter':{}})
    try:
        assert response.status == 200
        assert json.loads(response.readline())['stream_item'] == 'snapshot'
        frames = [json.loads(response.readline()) for _ in expected]
        assert [frame['event_id'] for frame in frames] == expected
        assert 'foreign-hidden' not in json.dumps(frames) and 'internal-unbound' not in json.dumps(frames)
    finally:
        response.close()
        connection.close()
    connection, response = open_native_observation(background_http, {
        'request_id':'reconnect-stream','filter':{},'cursor':frames[0]['cursor']})
    try:
        assert response.status == 200
        assert json.loads(response.readline())['stream_item'] == 'snapshot'
        assert json.loads(response.readline())['event_id'] == expected[1]
    finally:
        response.close()
        connection.close()
    assert services.host.conversation_store.session_tasks.require(task.task_id).status == 'queued'
