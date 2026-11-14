"""真实业务客户端往返核对执行来源、租约与错误事件，读取全程不改变租约。"""
import json

import pytest

from agent_py_agent.agent.conversation.claim_execution_binding import (
    ClaimExecutionBinding,
    bind_claim_execution,
    publish_claim_runtime_authority,
)
from agent_py_agent.cli.space_commands import BusinessClient
from agent_py_agent.tests.test_mega_business_api_actions import (
    background_http as background_http,
)
from agent_py_agent.tests.test_mega_business_api_actions import (
    native_request,
    observed_native_task,
    seed_observed_runtime,
)


def lifecycle_source(native):
    services, task = observed_native_task(native)
    repo, run = seed_observed_runtime(services, task)
    store = services.host.conversation_store
    claim = store.claims.acquire({'thread_id': task.target_thread_id,
                                 'reason': 'session_task', 'task_id': run['task_id']})
    binding = ClaimExecutionBinding(store.claims, task.target_thread_id, claim['claim_id'], task.target_thread_id)
    with bind_claim_execution(binding):
        publish_claim_runtime_authority({'invocation_run_id': 'observed-' + task.task_id,
                                         'run_id': 'observed-' + task.task_id, **run})

    def transport(path, request, **kwargs):
        status, _, raw = native_request(native, 'POST', path, request)
        return status, json.loads(raw)

    return BusinessClient(transport), services, task, repo, run


def test_execution_detail_links_source_claim_and_timeout_receipt(background_http):
    client, services, task, repo, run = lifecycle_source(background_http)
    repo.settle_agent_run(agent_run_id=run['agent_run_id'], attempt_id=run['attempt_id'],
        status='failed', payload={'error_code': 'MODEL_READ_TIMEOUT', 'message': 'api_key=synthetic-secret-value',
                                  'reason': 'model_read_deadline', 'retryable': True})
    store = services.host.conversation_store
    before = store.claims.load_report(task.target_thread_id)[0]
    params = {'run_id': 'observed-' + task.task_id, 'attempt_id': run['attempt_id']}
    reply = client.call('executions.get', params)
    assert reply['ok'], reply
    data = reply['data']
    assert data['execution']['status'] == 'failed'
    assert data['execution']['error']['code'] == 'MODEL_READ_TIMEOUT'
    assert data['execution']['error']['retryable'] is True
    assert 'synthetic-secret-value' not in json.dumps(data)
    lifecycle = data['lifecycle']
    assert lifecycle['source']['sender_thread_id'] == task.sender_thread_id
    assert lifecycle['source']['input_ref'] == task.body_guidance_id
    assert lifecycle['claim']['claim_id'] == before['claim_id']
    assert lifecycle['coverage'] == 'complete'
    assert any(row['kind'] == 'agent_run.completed' for row in lifecycle['events'])
    assert [row['attempt_id'] for row in lifecycle['attempts']] == [run['attempt_id']]
    assert store.claims.load_report(task.target_thread_id)[0] == before
    status, _, _ = native_request(background_http, 'POST', '/client/business',
        {'action': 'executions.get', 'request_id': 'foreign-execution', 'params': params}, user='business-bob')
    assert status != 200


def test_old_execution_cannot_borrow_new_claim_and_events_are_bounded(background_http):
    client, services, task, repo, run = lifecycle_source(background_http)
    store = services.host.conversation_store
    claim = store.claims.load_report(task.target_thread_id)[0]
    binding = ClaimExecutionBinding(store.claims, task.target_thread_id, claim['claim_id'], task.target_thread_id)
    # 同一主运行后续attempt有自己的租约证明，不能倒灌给旧attempt。
    later = repo.create_attempt(run['agent_run_id'])
    with bind_claim_execution(binding):
        publish_claim_runtime_authority({'invocation_run_id': 'observed-' + task.task_id,
            'run_id': 'observed-' + task.task_id, **run, 'attempt_id': later['attempt_id']})
    recovered = client.call('executions.get', {'run_id': 'observed-' + task.task_id,
                                             'attempt_id': run['attempt_id']})
    assert recovered['ok'], recovered
    assert recovered['data']['retry_refs'] == [later['attempt_id']]
    assert recovered['data']['execution']['status'] == 'cancelled'
    for index in range(102):
        repo.append_event(event_type='recovery.observed', agent_run_id=run['agent_run_id'],
            attempt_id=run['attempt_id'], task_run_id=run['task_run_id'], payload={'reason': str(index)})
    result = client.call('executions.get', {'run_id': 'observed-' + task.task_id,
                                          'attempt_id': run['attempt_id']})
    assert result['ok'], result
    lifecycle = result['data']['lifecycle']
    assert lifecycle['claim'] is None
    assert lifecycle['coverage'] == 'partial'
    assert set(lifecycle['unavailable']) == {'EXACT_CLAIM_UNAVAILABLE', 'HISTORY_WINDOW_TRUNCATED'}
    assert len(lifecycle['events']) == 100
    assert len(lifecycle['attempts']) == 2


def test_damaged_or_large_event_keeps_other_lifecycle_evidence(background_http):
    client, _, task, repo, run = lifecycle_source(background_http)
    large = repo.append_event(event_type='diagnostic', agent_run_id=run['agent_run_id'],
        attempt_id=run['attempt_id'], task_run_id=run['task_run_id'], payload={'reason': 'x' * 18000})
    bad = repo.append_event(event_type='diagnostic', agent_run_id=run['agent_run_id'],
        attempt_id=run['attempt_id'], task_run_id=run['task_run_id'], payload={})
    with repo._runtime_connection() as conn:
        conn.execute('UPDATE runtime_events SET payload_json=? WHERE event_id=?', ('{broken', bad['event_id']))
        conn.commit()
    result = client.call('executions.get', {'run_id': 'observed-' + task.task_id,
                                          'attempt_id': run['attempt_id']})
    assert result['ok'], result
    lifecycle = result['data']['lifecycle']
    assert lifecycle['coverage'] == 'partial' and lifecycle['claim'] is not None
    assert {'EVENT_PAYLOAD_TOO_LARGE', 'EVENT_PAYLOAD_INVALID'} <= set(lifecycle['unavailable'])
    assert {large['event_id'], bad['event_id']}.isdisjoint(row['event_id'] for row in lifecycle['events'])


@pytest.mark.parametrize(('status', 'code', 'expected'), [
    ('done', '', None),
    ('done', 'POST_EXECUTION_ARCHIVE_FAILED', 'POST_EXECUTION_ARCHIVE_FAILED'),
    ('failed', '', 'runner exited without confirmation'),
])
def test_successful_attempt_does_not_promote_runner_note_to_error(background_http, status, code, expected):
    client, _, task, repo, run = lifecycle_source(background_http)
    note = 'runner 本轮结束: completed' if status == 'done' else 'runner exited without confirmation'
    repo.settle_agent_run(agent_run_id=run['agent_run_id'], attempt_id=run['attempt_id'],
                          status=status, payload={'status': status, 'runtime_reason': note, 'error_code': code})
    result = client.call('executions.get', {'run_id': 'observed-' + task.task_id,
                                          'attempt_id': run['attempt_id']})
    assert result['ok'], result
    error = result['data']['execution']['error']
    assert (error['code'] if error else None) == expected
    assert any({'key': 'runtime_reason', 'value': note} in event['details']
               for event in result['data']['lifecycle']['events'])
    # 未确认的效果不能因为规范运行done而被涂成执行成功。
    assert result['data']['actual_outcome'] == 'unknown'


def test_messages_link_two_inputs_to_same_attempt_without_exclusive_tool_attribution(background_http):
    client, services, task, repo, run = lifecycle_source(background_http)
    store = services.host.conversation_store
    run_id = 'observed-' + task.task_id
    inputs = []
    for marker in ('first-user-input', 'second-user-input'):
        entry = store.guidance.append_once({'target_type': 'thread', 'target_id': task.target_thread_id,
            'message': marker, 'metadata': {'thread_id': task.target_thread_id, 'expected_turn_id': run_id}},
            dedupe_key='test-input-' + marker)
        assert store.guidance.claim_for_turn(entry, expected_turn_id=run_id, attempt_id=run['attempt_id'])
        store.guidance.submissions.mark_submitted(run_id, [entry], attempt_id=run['attempt_id'], provider_call_id='provider-' + marker)
        inputs.append(entry)
        store.messages.append({'thread_id': task.target_thread_id, 'role': 'host', 'content': marker,
            'metadata': {'guidance_id': entry.guidance_id}})
    store.messages.append({'thread_id': task.target_thread_id, 'role': 'assistant', 'content': 'both-results',
                           'metadata': {'conversation_request_id': 'background-turn-display-1',
                                        'dedupe_key': 'owner_delivery:background-wake:' + run_id}})
    params = {'target': {'kind': 'background_main', 'id': task.target_thread_id}, 'limit': 100}
    result = client.call('messages.list', params)
    assert result['ok'], result
    linked = [row for row in result['data']['items'] if row['text'] in {'first-user-input', 'second-user-input', 'both-results'}]
    expected = [{'run_id': run_id, 'attempt_id': run['attempt_id']}]
    assert len(linked) == 3 and all(row['execution_refs'] == expected for row in linked)
    assert {row['input_ref'] for row in linked if row['role'] == 'host'} == {entry.guidance_id for entry in inputs}
    assert all(row['association_reason'] == 'ATTEMPT_CONTEXT_NOT_EXCLUSIVE_TOOL_OWNERSHIP' for row in linked)
    reconnect = client.call('messages.list', params)
    assert reconnect['data']['items'] == result['data']['items']
    # 新attempt不能倒灌旧输入；缺少逐消息attempt的回复保持unknown，不能借latest补票。
    later = repo.create_attempt(run['agent_run_id'])
    after_retry = client.call('messages.list', params)
    assert after_retry['ok'], after_retry
    relevant = [row for row in after_retry['data']['items'] if row['text'] in {'first-user-input', 'second-user-input', 'both-results'}]
    assert all(row['execution_refs'] == expected for row in relevant if row['role'] == 'host')
    reply = next(row for row in relevant if row['role'] == 'assistant')
    assert reply['execution_refs'] == [] and reply['association_coverage'] == 'unknown'
    assert later['attempt_id'] not in json.dumps(relevant)
    # 伪造其他线程的输入引用不能借当前可见消息建立关联。
    created = client.call('background_agents.create', {'space_id': task.metadata['business_context']['space_id'],
                                                      'name': 'unrelated'})
    assert created['ok'], created
    other_id = created['data']['agent']['agent_id']
    store.messages.append({'thread_id': other_id, 'role': 'host', 'content': 'forged',
                           'metadata': {'guidance_id': inputs[0].guidance_id, 'conversation_request_id': run_id}})
    foreign = client.call('messages.list', {'target': {'kind': 'background_main', 'id': other_id}, 'limit': 100})
    assert foreign['ok'], foreign
    assert all(not row['execution_refs'] and row['input_ref'] is None for row in foreign['data']['items'])
