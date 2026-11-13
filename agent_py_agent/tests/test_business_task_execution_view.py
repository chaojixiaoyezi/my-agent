"""原生业务客户端复核任务活动执行及有界分页，不更改任务或租约。"""
import json

from agent_py_agent.agent.conversation.claim_execution_binding import (
    ClaimExecutionBinding,
    bind_claim_execution,
    publish_claim_runtime_authority,
)
from agent_py_agent.cli.space_commands import BusinessClient
from agent_py_agent.tests.test_mega_business_api_actions import (
    background_call,
    native_request,
    observed_native_task,
    seed_observed_runtime,
)
from agent_py_agent.tests.test_mega_business_api_actions import (
    background_http as background_http,
)


def active_task(native):
    services, task = observed_native_task(native)
    runtime, run = seed_observed_runtime(services, task)
    store = services.host.conversation_store
    request_id = 'observed-' + task.task_id
    store.session_tasks.bind_turn(task.task_id, turn_id=request_id)
    store.session_tasks.bind_execution(task.task_id, task_run_id=run['task_run_id'],
                                       target_thread_id=task.target_thread_id)
    # 原控制会话按线程领取，claim.task_id 合法为空；执行身份来自随后发表的绑定。
    claim = store.claims.acquire({'thread_id': task.target_thread_id, 'reason': 'session_task'})
    binding = ClaimExecutionBinding(store.claims, task.target_thread_id, claim['claim_id'], task.target_thread_id)
    with bind_claim_execution(binding):
        publish_claim_runtime_authority({'invocation_run_id': request_id, 'run_id': request_id, **run})

    def transport(path, request, **kwargs):
        status, _, raw = native_request(native, 'POST', path, request)
        return status, json.loads(raw)

    return BusinessClient(transport), services, task, runtime, run


def test_tasks_use_live_exact_execution_without_rewriting_claim(background_http, monkeypatch):
    client, services, task, _, _ = active_task(background_http)
    store = services.host.conversation_store
    claim_before = store.claims.load_report(task.target_thread_id)[0]
    assert not claim_before['task_id']
    before = store.session_tasks.require(task.task_id).to_dict()

    def forbid_scan(*args, **kwargs):
        raise AssertionError('任务详情和分页不得扫描全部历史任务')

    monkeypatch.setattr(store.session_tasks, 'list_report', forbid_scan)
    result = client.call('tasks.get', {'task_id': task.task_id})
    assert result['ok'], result
    assert result['data']['task']['current_run_id'] == 'observed-' + task.task_id
    assert result['data']['scheduling']['state'] == 'running'
    listed = client.call('tasks.list', {'limit': 1})
    assert listed['ok'], listed
    assert listed['data']['items'][0]['current_run_id'] == 'observed-' + task.task_id
    assert store.claims.load_report(task.target_thread_id)[0] == claim_before
    assert store.session_tasks.require(task.task_id).to_dict() == before


def test_old_claim_attempt_is_unknown_and_terminal_task_stays_terminal(background_http):
    client, services, task, runtime, run = active_task(background_http)
    runtime.create_attempt(run['agent_run_id'])
    result = client.call('tasks.get', {'task_id': task.task_id})
    assert result['ok'], result
    assert result['data']['task']['current_run_id'] is None
    assert result['data']['scheduling']['state'] == 'unknown'
    assert result['data']['scheduling']['reason_code'] == 'EXECUTION_AUTHORITY_UNAVAILABLE'
    from agent_py_agent.agent.conversation.session_tasks import SessionTaskUpdate
    services.host.conversation_store.session_tasks.advance(task.task_id,
        SessionTaskUpdate(status='failed', failure_code='TEST_FINISHED'))
    terminal = client.call('tasks.get', {'task_id': task.task_id})
    assert terminal['ok'], terminal
    assert terminal['data']['scheduling']['state'] == 'terminal'
    assert terminal['data']['task']['current_run_id'] is None


def test_tasks_list_continues_original_index_cursor(background_http):
    _, task = observed_native_task(background_http)
    for index in range(3):
        status, reply = background_call(background_http, 'tasks.assign', {
            'agent_id': task.target_thread_id,
            'space_id': task.metadata['business_context']['space_id'],
            'instruction': f'独立测试工作 {index}'}, idempotency_key=f'task-page-{index}')
        assert status == 200, reply
    cursor, seen = None, set()
    for _ in range(4):
        params = {'limit': 1, **({'cursor': cursor} if cursor else {})}
        status, reply = background_call(background_http, 'tasks.list', params)
        assert status == 200, reply
        assert len(reply['data']['items']) == 1
        [item] = reply['data']['items']
        assert item['task_id'] not in seen
        seen.add(item['task_id'])
        cursor = reply['data']['next_cursor']
    assert cursor is None and len(seen) == 4


def test_detached_task_uses_its_exact_claim_scope(background_http):
    from agent_py_agent.agent.conversation.run_claim import detached_task_claim_scope_id

    services, task = observed_native_task(background_http)
    runtime, run = seed_observed_runtime(services, task)
    store = services.host.conversation_store
    request_id = 'observed-' + task.task_id
    store.session_tasks.bind_turn(task.task_id, turn_id=request_id)
    store.session_tasks.bind_execution(task.task_id, task_run_id=run['task_run_id'],
                                       target_thread_id=task.target_thread_id)
    store.tasks.bind({'thread_id': task.target_thread_id, 'task_id': task.task_id,
                      'cancellation_scope': 'detached'})
    scope = detached_task_claim_scope_id(task.target_thread_id, task.task_id)
    claim = store.claims.acquire({'thread_id': task.target_thread_id, 'reason': 'session_task',
                                  'claim_scope_id': scope})
    with bind_claim_execution(ClaimExecutionBinding(store.claims, task.target_thread_id, claim['claim_id'], scope)):
        publish_claim_runtime_authority({'invocation_run_id': request_id, 'run_id': request_id, **run})
    status, reply = background_call(background_http, 'tasks.get', {'task_id': task.task_id})
    assert status == 200, reply
    assert reply['data']['task']['current_run_id'] == request_id
    assert reply['data']['scheduling']['state'] == 'running'
    assert not store.claims.load_report(task.target_thread_id)[0]
