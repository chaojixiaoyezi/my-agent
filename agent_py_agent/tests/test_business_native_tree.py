"""原生业务树覆盖总控直接执行、空间过滤和有界线程发现。"""
from agent_py_agent.tests.test_mega_business_api_actions import (
    background_call,
    seed_observed_runtime,
)
from agent_py_agent.tests.test_mega_business_api_actions import (
    background_http as background_http,
)


def test_native_tree_includes_controller_without_persistent_background_seat(background_http, monkeypatch):
    status, space = background_call(background_http, 'spaces.create', {'name': '直接执行空间'},
                                   idempotency_key='tree-space')
    assert status == 200, space
    space_id = space['data']['space_id']
    status, submitted = background_call(background_http, 'tasks.submit',
        {'space_id': space_id, 'instruction': '总控执行并委派子任务'}, idempotency_key='tree-task')
    assert status == 200, submitted
    [services] = background_http[0]._business_services.values()
    store = services.host.conversation_store
    task = store.session_tasks.require(submitted['data']['task_id'])
    from agent_py_agent.agent.runtime_db.repository import RuntimeRepository
    from agent_py_agent.agent.runtime_db.schema import runtime_db_path

    runtime = RuntimeRepository(runtime_db_path(services.owner_home))
    for index in range(110):
        runtime.record_run_creation(owner_id=services.owner_id, run_id=f'other-space-{index}',
                                    thread_id=task.target_thread_id)
    runtime, _ = seed_observed_runtime(services, task)
    run_id = 'observed-' + task.task_id
    runtime.record_run_creation(owner_id=services.owner_id, run_id='tree-child',
                                thread_id=task.target_thread_id, parent_run_id=run_id)

    def reject_full_scan(*args, **kwargs):
        raise AssertionError('业务树不能扫描全部历史线程')

    monkeypatch.setattr(type(store.threads), 'list_report', reject_full_scan)
    status, tree = background_call(background_http, 'agent_tree.get', {
        'space_id': space_id, 'root_agent_id': task.target_thread_id, 'limit': 30})
    assert status == 200, tree
    nodes = {row['id']: row for row in tree['data']['nodes']}
    assert nodes[task.target_thread_id]['kind'] == 'controller'
    assert nodes[run_id]['task_id'] == task.task_id
    assert nodes['tree-child']['parent_run_id'] == run_id
    assert {'from_id': task.target_thread_id, 'to_id': run_id, 'relation': 'executes'} in tree['data']['edges']
    assert {'from_id': run_id, 'to_id': 'tree-child', 'relation': 'child_of'} in tree['data']['edges']


def test_unassigned_tree_only_returns_unassigned_executions(background_http):
    status, space = background_call(background_http, 'spaces.create', {'name': '空间限定'}, idempotency_key='scope-space')
    assert status == 200, space
    status, response = background_call(background_http, 'tasks.submit', {
        'space_id': space['data']['space_id'], 'instruction': '空间内任务'}, idempotency_key='scope-task')
    assert status == 200, response
    [services] = background_http[0]._business_services.values()
    task = services.host.conversation_store.session_tasks.require(response['data']['task_id'])
    repo, _ = seed_observed_runtime(services, task)
    repo.record_run_creation(owner_id=services.owner_id, run_id='unassigned-work', thread_id=task.target_thread_id)
    status, listing = background_call(background_http, 'agent_tree.get', {
        'space_id': None, 'root_agent_id': task.target_thread_id, 'include_completed': True})
    assert status == 200, listing
    ids = {row['id'] for row in listing['data']['nodes']}
    assert 'unassigned-work' in ids and 'observed-' + task.task_id not in ids

