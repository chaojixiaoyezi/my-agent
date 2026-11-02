"""真实原会话/运行库的只读投影；模型、调度与处理回执不由观察推进。"""
import hashlib
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.conversation.background_team_lifecycle import BACKGROUND_IDENTITY_KEY
from agent_py_agent.agent.conversation.business_access import BusinessActor, BusinessError
from agent_py_agent.agent.conversation.business_contracts import (
    BusinessContextIntent,
    BusinessExecutionContext,
)
from agent_py_agent.agent.conversation.business_observability import (
    BusinessObservationService,
    ObservationPage,
    ObservationRecord,
)
from agent_py_agent.agent.conversation.models import ConversationThread
from agent_py_agent.agent.conversation.session_tasks import SessionTaskDraft
from agent_py_agent.agent.conversation.store import ConversationStore
from agent_py_agent.agent.conversation.store_threads import create_thread_atomic
from agent_py_agent.agent.runtime_db.repository import RuntimeRepository


def fixture(tmp_path):
    store = ConversationStore(tmp_path / 'conversations')
    owner = 'local/test'
    for identifier in ('controller', 'background'):
        metadata = ({'business_controller': True} if identifier == 'controller' else {
            BACKGROUND_IDENTITY_KEY: {'agent_id': identifier, 'managed_by_thread_id': 'controller',
                'space_id': 'space-a', 'project_ref': None, 'identity_state': 'available',
                'after_task': 'reuse', 'identity_revision': 1}})
        create_thread_atomic(store.threads, ConversationThread(thread_id=identifier, owner_id=owner,
            canonical_user_id=owner, title=identifier, metadata=metadata))
    intent = BusinessContextIntent(owner, 'space-a', None, None)
    task = store.session_tasks.create(SessionTaskDraft('controller', 'background', '观察测试', dedupe_key='one'),
        business_context_intent=intent,
        business_context_factory=lambda tid: BusinessExecutionContext(owner, 'space-a', None, tid, 1, None, (), (), 'business-context.v1'))
    runtime = RuntimeRepository(tmp_path / 'runtime.db')
    root = runtime.record_run_creation(owner_id=owner, run_id='root-run', thread_id='background',
        task_run_metadata={'business_context': task.metadata['business_context'], 'session_task_ref': task.task_id})
    child = runtime.record_run_creation(owner_id=owner, run_id='child-run', thread_id='background', parent_run_id='root-run')
    service = BusinessObservationService(tmp_path, owner_id=owner, store=store, runtime=runtime, cursor_key=b'x' * 32)
    allowed = [True]
    actor = BusinessActor(owner, 'user', service, lambda _action, _facts: allowed[0])
    return SimpleNamespace(store=store, runtime=runtime, task=task, root=root, child=child,
        service=service, actor=actor, owner=owner, allowed=allowed)


def test_tasks_and_executions_are_exact_readonly_projections(tmp_path):
    case = fixture(tmp_path)
    before = hashlib.sha256(case.runtime.db_path.read_bytes()).hexdigest()
    tasks = case.service.query('tasks.list', {'filter': {'space_id': 'space-a'}}, actor=case.actor)
    assert isinstance(tasks, dict), '任务观察必须返回结构化投影'
    assert [row['task_id'] for row in tasks['items']] == [case.task.task_id]
    assert tasks['coverage'] == 'complete'
    executions = case.service.query('executions.list', {'filter': {'space_id': 'space-a'}}, actor=case.actor)
    assert {row['run_id'] for row in executions['items']} == {'root-run', 'child-run'}
    exact = case.service.query('executions.get', {'run_id': 'root-run', 'attempt_id': case.root['attempt_id']}, actor=case.actor)
    assert exact['execution']['attempt_id'] == case.root['attempt_id']
    assert case.store.session_tasks.require(case.task.task_id).status == 'queued'
    assert hashlib.sha256(case.runtime.db_path.read_bytes()).hexdigest() == before


def test_tree_keeps_organization_and_execution_edges_separate(tmp_path):
    case = fixture(tmp_path)
    result = case.service.query('agent_tree.get', {'space_id': 'space-a', 'include_completed': True}, actor=case.actor)
    assert isinstance(result, dict), '全树观察必须返回结构化投影'
    assert {row['id'] for row in result['nodes']} == {'controller', 'background', 'root-run', 'child-run'}
    assert {'from_id': 'controller', 'to_id': 'background', 'relation': 'manages'} in result['edges']
    assert {'from_id': 'background', 'to_id': 'root-run', 'relation': 'executes'} in result['edges']
    assert {'from_id': 'root-run', 'to_id': 'child-run', 'relation': 'child_of'} in result['edges']
    assert not any(edge['relation'] == 'child_of' and edge['from_id'] == 'controller' for edge in result['edges'])


def test_live_access_and_owner_filter_do_not_trust_cursor(tmp_path):
    case = fixture(tmp_path)
    first = case.service.query('executions.list', {'limit': 1}, actor=case.actor)
    assert isinstance(first, dict), '执行分页必须返回结构化投影'
    assert first['next_cursor']
    case.allowed[0] = False
    with pytest.raises(BusinessError, match='ACCESS_DENIED'):
        case.service.query('executions.list', {'limit': 1, 'cursor': first['next_cursor']}, actor=case.actor)


def test_absent_sources_and_unknown_cost_are_not_empty_success(tmp_path):
    service = BusinessObservationService(tmp_path, owner_id='local/test')
    actor = BusinessActor('local/test', 'user', service, lambda _a, _f: True)
    result = service.query('executions.list', {}, actor=actor)
    assert isinstance(result, dict), '缺来源也须返回明确覆盖状态'
    assert result['items'] == [] and result['coverage'] == 'unknown'
    with pytest.raises(BusinessError, match='HOST_NOT_READY'):
        service.query('usage.get', {}, actor=actor)
    service.readers['resource_usage'] = lambda _p: ObservationPage((ObservationRecord({
        'active_model_calls': 0, 'queued_tasks': 0, 'active_subagents': 0,
        'rss_bytes': None, 'source': 'explicit_host_meter'}, 'local/test'),))
    usage = service.query('usage.get', {}, actor=actor)
    assert usage['actual']['cost'] is None and usage['estimated']['cost'] is None
    assert usage['unknown']


def test_extra_source_rows_are_scope_checked_and_redacted(tmp_path):
    public = {'operation_id': 'op-1', 'run_id': 'r', 'attempt_id': 'a', 'task_id': 't',
        'tool_name': 'read_file', 'status': 'SUCCEEDED', 'executed': True, 'effect_outcome': 'committed',
        'duration_ms': 1, 'error': None, 'result_ref': 'result-1'}
    rows = (ObservationRecord(public, 'local/test', space_id='space-a'),
            ObservationRecord({**public, 'operation_id': 'other'}, 'local/other', space_id='space-a'))
    service = BusinessObservationService(tmp_path, owner_id='local/test', readers={
        'tool_calls': lambda _params: ObservationPage(rows, revision='rev-1')})
    actor = BusinessActor('local/test', 'u', service, lambda _a, _f: True)
    result = service.query('tool_calls.list', {'filter': {'space_id': 'space-a'}}, actor=actor)
    assert isinstance(result, dict), '工具观察必须返回结构化投影'
    assert [row['operation_id'] for row in result['items']] == ['op-1']


@pytest.mark.parametrize(('action', 'params', 'value'), [
    ('tasks.get', {'task_id': 'task'}, {'task': 'TaskView', 'assignments': [], 'workflow_ref': 'workflow', 'result_refs': [], 'allowed_actions': []}),
    ('tool_calls.get', {'operation_id': 'id'}, {'tool_call': 'ToolCallView', 'redacted_input': {'api_key': 'private-value'}, 'result_ref': 'result', 'effect_outcome': 'unknown'}),
    ('artifacts.list', {}, {'items': [], 'next_cursor': None, 'coverage': 'complete'}),
    ('artifacts.get', {'artifact_id': 'id'}, {'artifact': 'ArtifactView', 'version_ref': 'id', 'read_ref': 'read-id', 'available': False}),
    ('approvals.list', {}, {'items': [], 'next_cursor': None, 'coverage': 'complete'}),
    ('approvals.get', {'approval_id': 'id'}, {'approval': 'ApprovalView', 'expires_at': None, 'allowed_actions': []}),
    ('changes.get', {'task_id': 'task'}, {'changes': [], 'evidence_refs': [], 'coverage': 'partial'}),
    ('permissions.get', {'target': {'kind': 'background_main', 'id': 'thread'}},
        {'effective_scope': 'ResourceScope', 'unavailable': [], 'sources': []}),
    ('logs.read', {'source_ref': 'log'}, {'chunks': [], 'next_cursor': None, 'coverage': 'expired', 'truncated': False,
        'gap': {'reason_code': 'RETENTION', 'from_cursor': None, 'to_cursor': None, 'reset_required': True}}),
])
def test_host_detail_ports_preserve_closed_result_and_redaction(tmp_path, action, params, value):
    from agent_py_agent.agent.plugin_host.business_action_specs import (
        business_action_spec,
        business_defs,
    )

    definitions = business_defs()

    def sample(shape):
        if '$ref' in shape:
            return sample(definitions[shape['$ref'].split('/')[-1]])
        if 'const' in shape:
            return shape['const']
        if 'enum' in shape:
            return shape['enum'][0]
        if 'anyOf' in shape:
            return sample(next((x for x in shape['anyOf'] if x.get('type') == 'null'), shape['anyOf'][0]))
        kind = shape.get('type')
        if kind == 'object':
            return {key: sample(shape['properties'][key]) for key in shape.get('required', [])}
        if kind == 'array':
            return []
        if kind == 'integer' or kind == 'number':
            return shape.get('minimum', 0)
        if kind == 'boolean':
            return False
        if kind == 'null':
            return None
        return 'a' * 64 if shape.get('pattern') == '^[0-9a-f]{64}$' else 'id'

    value = {key: sample(definitions[item]) if isinstance(item, str) and item in definitions else item for key, item in value.items()}
    if 'task' in value:
        value['task']['task_id'] = 'task'
    service = BusinessObservationService(tmp_path, owner_id='local/test', readers={action: lambda _p:
        ObservationPage((ObservationRecord(value, 'local/test', task_id='task', thread_id='thread'),))})
    actor = BusinessActor('local/test', 'u', service, lambda _a, _f: True)
    try:
        result = service.query(action, params, actor=actor)
    except BusinessError as error:
        result = {'error': error.code}
    assert 'error' not in result, result
    assert business_action_spec(action).validate_result(result) == result
    if action == 'tool_calls.get':
        assert 'private-value' not in str(result)
    if action == 'logs.read':
        assert result['coverage'] == 'expired' and result['gap']['reset_required'] is True


def test_subagent_queries_use_exact_original_parent(tmp_path):
    case = fixture(tmp_path)
    try:
        result = case.service.query('subagents.list', {'parent_run_id': 'root-run'}, actor=case.actor)
    except BusinessError as error:
        result = {'error': error.code}
    assert 'error' not in result
    assert [row['run_id'] for row in result['items']] == ['child-run']
    detail = case.service.query('subagents.get', {'run_id': 'child-run'}, actor=case.actor)
    assert detail['subagent']['parent_run_id'] == 'root-run'


def test_cursor_rejects_changed_filter_and_tree_can_re_root_deep_descendants(tmp_path):
    case = fixture(tmp_path)
    first = case.service.query('executions.list', {'limit': 1, 'filter': {'space_id': 'space-a'}}, actor=case.actor)
    with pytest.raises(BusinessError, match='CURSOR_EXPIRED'):
        case.service.query('executions.list', {'limit': 1, 'filter': {'space_id': 'other'}, 'cursor': first['next_cursor']}, actor=case.actor)
    shallow = case.service.query('agent_tree.get', {'space_id': 'space-a', 'max_depth': 1}, actor=case.actor)
    assert shallow['has_more_descendants'] is True
    deeper = case.service.query('agent_tree.get', {'space_id': 'space-a', 'root_agent_id': 'root-run', 'max_depth': 1}, actor=case.actor)
    assert {row['id'] for row in deeper['nodes']} == {'root-run', 'child-run'}


def subscription_fixture(tmp_path):
    from agent_py_agent.agent.conversation.business_spaces import BusinessSpaceService
    from agent_py_agent.agent.conversation.managed_subscriptions import (
        ManagedSubscriptionService,
        SourceIdentity,
    )

    case = fixture(tmp_path)
    catalog = BusinessSpaceService(tmp_path, owner_id=case.owner, enabled=True)
    actor = BusinessActor(case.owner, 'user', catalog, lambda _a, _f: case.allowed[0])
    space = catalog.create({'name': '订阅空间'}, actor=actor, idempotency_key='make-space')['space_id']
    class OriginalTaskPort:
        def submit(self, request: dict, *, actor: object, idempotency_key: str) -> dict:
            intent = BusinessContextIntent(case.owner, request['space_id'], None, None)
            task = case.store.session_tasks.create(SessionTaskDraft('controller', 'background', request['instruction'], dedupe_key=idempotency_key),
                business_context_intent=intent, business_context_factory=lambda tid: BusinessExecutionContext(
                    case.owner, request['space_id'], None, tid, 0, None, (), (), 'business-context.v1'))
            return {'task_id': task.task_id, 'request_id': task.task_id, 'operation_id': task.task_id, 'state': 'accepted'}
    source = {'owner_id': case.owner, 'source_ref': 'comments', 'connection_state': 'connected'}
    service = ManagedSubscriptionService(catalog, background_service=OriginalTaskPort(),
        source_resolver=lambda _ref, _actor: dict(source), source_authorizer=lambda _identity, _row, _actor: case.allowed[0],
        submit_actor_provider=lambda current, _row: current)
    return SimpleNamespace(**vars(case), subscriptions=service, catalog=catalog, subscription_actor=actor,
        space=space, source=source, source_identity=SourceIdentity(case.owner, 'comments', 'signed-source'))


def test_subscription_revision_identity_deduplicates_original_tasks(tmp_path):
    from agent_py_agent.agent.conversation.managed_subscriptions import SourceEvent
    case = subscription_fixture(tmp_path)
    created = case.subscriptions.execute('subscriptions.create', {'space_id': case.space, 'source_ref': 'comments'},
        actor=case.subscription_actor, idempotency_key='create-subscription')
    assert isinstance(created, dict), '订阅必须和原catalog同笔保存'
    sub = created['subscription']
    first = SourceEvent('comments', 'comment-1', 1, '第一版')
    receipt = case.subscriptions.accept_event(sub['subscription_id'], first, actor=case.subscription_actor, source_identity=case.source_identity)
    duplicate = case.subscriptions.accept_event(sub['subscription_id'], first, actor=case.subscription_actor, source_identity=case.source_identity)
    assert receipt['task_id'] == duplicate['task_id']
    edited = case.subscriptions.accept_event(sub['subscription_id'], SourceEvent('comments', 'comment-1', 2, '编辑版'),
        actor=case.subscription_actor, source_identity=case.source_identity)
    assert edited['task_id'] != receipt['task_id']
    tasks, errors = case.store.session_tasks.list_report(limit=0)
    assert not errors and len(tasks) == 3  # 夹具原任务 + 两个来源revision。
    assert all(task.status == 'queued' for task in tasks)
    assert all(task.metadata['business_context']['space_id'] == case.space for task in tasks if task.task_id != case.task.task_id)


def test_subscription_pause_restore_remove_and_cas_are_real_catalog_operations(tmp_path):
    from agent_py_agent.agent.conversation.managed_subscriptions import SourceEvent
    case = subscription_fixture(tmp_path)
    request = {'space_id': case.space, 'source_ref': 'comments', 'routing_instruction': '分类后整理'}
    created = case.subscriptions.execute('subscriptions.create', request, actor=case.subscription_actor, idempotency_key='create-sub')
    assert isinstance(created, dict)
    replay = case.subscriptions.execute('subscriptions.create', request, actor=case.subscription_actor, idempotency_key='create-sub')
    assert replay == created
    sub = created['subscription']
    paused = case.subscriptions.execute('subscriptions.pause', {'subscription_id': sub['subscription_id']}, actor=case.subscription_actor,
        expected_revision=sub['revision'], idempotency_key='pause-sub')
    assert paused['state'] == 'paused'
    with pytest.raises(BusinessError, match='SUBSCRIPTION_NOT_ACTIVE'):
        case.subscriptions.accept_event(sub['subscription_id'], SourceEvent('comments', 'c', 1, '正文'),
            actor=case.subscription_actor, source_identity=case.source_identity)
    with pytest.raises(BusinessError, match='REVISION_CONFLICT'):
        case.subscriptions.execute('subscriptions.resume', {'subscription_id': sub['subscription_id']}, actor=case.subscription_actor,
            expected_revision=sub['revision'], idempotency_key='stale-resume')
    resumed = case.subscriptions.execute('subscriptions.resume', {'subscription_id': sub['subscription_id']}, actor=case.subscription_actor,
        expected_revision=paused['subscription']['revision'], idempotency_key='resume-sub')
    removed = case.subscriptions.execute('subscriptions.remove', {'subscription_id': sub['subscription_id']}, actor=case.subscription_actor,
        expected_revision=resumed['subscription']['revision'], idempotency_key='remove-sub')
    assert removed['state'] == 'removed'
    assert case.subscriptions.execute('subscriptions.list', {}, actor=case.subscription_actor)['items'][0]['state'] == 'removed'
    assert not (tmp_path / 'data' / 'subscriptions').exists(), '不能建立第二配置账'


def test_subscription_pagination_get_update_and_connection_revocation(tmp_path):
    from agent_py_agent.agent.conversation.managed_subscriptions import SourceEvent
    case = subscription_fixture(tmp_path)
    for key in ('first', 'second'):
        case.subscriptions.execute('subscriptions.create', {'space_id': case.space, 'source_ref': 'comments'},
            actor=case.subscription_actor, idempotency_key=key)
    try:
        page = case.subscriptions.execute('subscriptions.list', {'limit': 1}, actor=case.subscription_actor)
    except AttributeError:
        page = {'error': 'missing_original_cursor_port'}
    assert 'error' not in page, page
    assert page['next_cursor'] and page['coverage'] == 'partial'
    next_page = case.subscriptions.execute('subscriptions.list', {'limit': 1, 'cursor': page['next_cursor']}, actor=case.subscription_actor)
    assert next_page['items'][0]['subscription_id'] != page['items'][0]['subscription_id']
    sub = page['items'][0]
    detail = case.subscriptions.execute('subscriptions.get', {'subscription_id': sub['subscription_id']}, actor=case.subscription_actor)
    assert detail['subscription'] == sub
    updated = case.subscriptions.execute('subscriptions.update', {'subscription_id': sub['subscription_id'], 'routing_instruction': '新路线'},
        actor=case.subscription_actor, expected_revision=0, idempotency_key='update-route')
    assert updated['revision'] == 1
    case.source['connection_state'] = 'not_connected'
    with pytest.raises(BusinessError, match='SOURCE_NOT_CONNECTED'):
        case.subscriptions.accept_event(sub['subscription_id'], SourceEvent('comments', 'event', 1, '内容'),
            actor=case.subscription_actor, source_identity=case.source_identity)


def test_messages_read_original_history_and_do_not_rebind_cursor(tmp_path):
    case = fixture(tmp_path)
    first = case.store.messages.append({'thread_id': 'background', 'role': 'assistant', 'content': '第一条',
        'metadata': {'task_id': case.task.task_id}})
    second = case.store.messages.append({'thread_id': 'background', 'role': 'user', 'content': '第二条',
        'metadata': {'task_id': case.task.task_id}})
    params = {'target': {'kind': 'background_main', 'id': 'background'}, 'limit': 1}
    try:
        page = case.service.query('messages.list', params, actor=case.actor)
    except BusinessError as error:
        page = {'error': error.code}
    assert 'error' not in page, page
    assert page['items'][0]['message_id'] in {first.message_id, second.message_id}
    cursor = page['next_cursor']
    assert cursor
    other = case.service.query('messages.list', {**params, 'cursor': cursor}, actor=case.actor)
    assert {page['items'][0]['message_id'], other['items'][0]['message_id']} == {first.message_id, second.message_id}
    with pytest.raises(BusinessError, match='CURSOR_EXPIRED'):
        case.service.query('messages.list', {**params, 'target': {'kind': 'controller', 'id': 'controller'}, 'cursor': cursor}, actor=case.actor)
    denied = BusinessActor(case.owner, 'u', case.service, lambda _a, facts: facts.get('thread_id') != 'background')
    with pytest.raises(BusinessError, match='ACCESS_DENIED'):
        case.service.query('messages.list', params, actor=denied)


def test_replaced_message_source_expires_old_cursor(tmp_path):
    case = fixture(tmp_path)
    for content in ('first', 'second'):
        case.store.messages.append({'thread_id': 'background', 'role': 'assistant', 'content': content})
    params = {'target': {'kind': 'background_main', 'id': 'background'}, 'limit': 1}
    page = case.service.query('messages.list', params, actor=case.actor)
    source = case.store.storage.message_path('background')
    old = source.with_suffix('.old')
    source.rename(old)
    source.write_bytes(old.read_bytes())
    try:
        case.service.query('messages.list', {**params, 'cursor': page['next_cursor']}, actor=case.actor)
    except BusinessError as error:
        observed = error.code
    else:
        observed = 'OLD_CURSOR_ACCEPTED'
    assert observed == 'CURSOR_EXPIRED'


def test_incomplete_detail_source_cannot_claim_a_completed_read(tmp_path):
    data = {'changes': [], 'evidence_refs': [], 'coverage': 'complete'}
    service = BusinessObservationService(tmp_path, owner_id='local/test', readers={
        'changes.get': lambda _p: ObservationPage((ObservationRecord(data, 'local/test', task_id='task'),), coverage='unknown')})
    actor = BusinessActor('local/test', 'u', service, lambda _a, _f: True)
    result = service.query('changes.get', {'task_id': 'task'}, actor=actor)
    assert result['coverage'] != 'complete'


@pytest.mark.parametrize('workflow', [None, 'workflow-recorded-exactly'])
def test_task_detail_preserves_nullable_canonical_workflow_and_closed_dto(tmp_path, workflow):
    from agent_py_agent.agent.gateway_parts.io import update_json_file_atomic
    from agent_py_agent.agent.plugin_host.business_action_specs import business_action_spec
    from agent_py_agent.agent.plugin_host.errors import HostAPIError

    case = fixture(tmp_path)
    if workflow is not None:
        # 只准备原规范记录，不向观察器注入详情或伪造关联返回值。
        update_json_file_atomic(case.store.session_tasks._path(case.task.task_id), lambda value:
            {**value, 'metadata': {**value['metadata'], 'workflow_ref': workflow}}, require_existing=True)
    try:
        result = case.service.query('tasks.get', {'task_id': case.task.task_id}, actor=case.actor)
    except BusinessError as error:
        result = {'error': error.code}
    assert 'error' not in result, result
    assert result['workflow_ref'] == workflow
    assert result['task']['task_id'] == result['assignments'][0]['task_id'] == case.task.task_id
    spec = business_action_spec('tasks.get')
    assert spec.validate_result(result) == result
    for invalid in ({k: v for k, v in result.items() if k != 'workflow_ref'},
                    {**result, 'unexpected': True}, {**result, 'workflow_ref': ''}):
        with pytest.raises(HostAPIError):
            spec.validate_result(invalid)
    assert case.store.session_tasks.require(case.task.task_id).metadata.get('workflow_ref') == workflow


def close_observed_task_run(runtime, runs, *, explicit=False):
    """用原结算入口关闭精确树，不手工拼造关闭事件。"""
    for run in runs:
        assert runtime.settle_agent_run(agent_run_id=run['agent_run_id'], attempt_id=run['attempt_id'],
            status='done')['settled'] is True
    root = runs[0]
    settle = runtime.settle_task_run_terminal if explicit else runtime.settle_task_run_if_agent_tree_terminal
    assert settle(task_run_id=root['task_run_id'], task_id=root['task_id'],
        reason='private-close-reason', **({'status': 'done'} if explicit else {}))['settled'] is True
    with runtime._runtime_connection() as conn:
        return dict(conn.execute("SELECT * FROM runtime_events WHERE event_type='task_run.closed' "
            'AND task_run_id=?', (root['task_run_id'],)).fetchone())


def test_run_event_with_empty_optional_attempt_does_not_wait_for_a_new_attempt(tmp_path):
    case = fixture(tmp_path)
    event = case.runtime.append_event(event_type='agent_run.progress', attempt_id='', agent_run_id=case.root['agent_run_id'],
        task_run_id=case.root['task_run_id'])
    page = case.service.query('events.list', {'filter': {}}, actor=case.actor)
    assert page['coverage'] == 'complete'
    [view] = [row for row in page['events'] if row['event_id'] == event['event_id']]
    assert view['attempt_id'] is None and view['run_id'] == 'root-run'
    assert view['node'] == {'kind': 'background_main', 'id': 'root-run'}
    assert view['payload']['thread_id'] == 'background'


def event_position(case, cursor, filters):
    scope = case.service.scope_digest('events.list', {'filter': filters}, case.actor)
    return case.service.cursor_position(cursor, scope)['after']


@pytest.mark.parametrize('explicit', [False, True])
def test_task_run_close_is_delivered_and_cursor_reaches_the_next_run(tmp_path, explicit):
    case = fixture(tmp_path)
    filters = {'space_id': 'space-a'}
    first = case.service.query('events.list', {'filter': filters}, actor=case.actor)
    closed = close_observed_task_run(case.runtime, [case.root, case.child], explicit=explicit)
    later = case.runtime.record_run_creation(owner_id=case.owner, run_id='later-run', thread_id='background',
        task_run_metadata={'business_context': case.task.metadata['business_context'], 'session_task_ref': case.task.task_id})
    before = case.runtime.db_path.read_bytes()
    page = case.service.query('events.list', {'filter': filters, 'cursor': first['next_cursor']}, actor=case.actor)
    by_id = {row['event_id']: row for row in page['events']}
    assert closed['event_id'] in by_id, page
    close = by_id[closed['event_id']]
    assert close['event_type'] == 'task_run.closed'
    assert close['node'] is None and close['run_id'] is None and close['attempt_id'] is None
    assert close['task_id'] == case.task.task_id and close['space_id'] == 'space-a'
    assert close['payload']['status'] == 'done'
    assert close['payload']['thread_id'] == 'background'
    assert 'private-close-reason' not in str(page)
    assert any(row['attempt_id'] == later['attempt_id'] for row in page['events'])
    positions = [event_position(case, row['cursor'], filters) for row in page['events']]
    assert positions == sorted(set(positions))
    assert event_position(case, first['next_cursor'], filters) < positions[0]
    resumed = case.service.query('events.list', {'filter': filters, 'cursor': close['cursor']}, actor=case.actor)
    assert [row['event_id'] for row in resumed['events']] == [
        row['event_id'] for row in page['events'] if event_position(case, row['cursor'], filters) > closed['seq']]
    end = case.service.query('events.list', {'filter': filters, 'cursor': page['next_cursor']}, actor=case.actor)
    assert end['events'] == [] and end['coverage'] == 'complete'
    assert event_position(case, end['next_cursor'], filters) == positions[-1]
    assert case.runtime.db_path.read_bytes() == before


@pytest.mark.parametrize('extra,visible', [({}, True), ({'agent_id': 'background'}, True),
    ({'project_ref': 'other-project'}, False), ({'space_id': 'other-space'}, False),
    ({'task_id': 'other-task'}, False), ({'run_id': 'root-run'}, False),
    ({'attempt_id': 'not-an-attempt'}, False), ({'agent_id': 'other-thread'}, False)])
def test_task_run_close_intersects_every_requested_filter(tmp_path, extra, visible):
    case = fixture(tmp_path)
    closed = close_observed_task_run(case.runtime, [case.root, case.child])
    filters = {'space_id': 'space-a', **extra}
    page = case.service.query('events.list', {'filter': filters}, actor=case.actor)
    assert (closed['event_id'] in {row['event_id'] for row in page['events']}) is visible
    assert page['coverage'] == 'complete'
    assert event_position(case, page['next_cursor'], filters) == closed['seq']


@pytest.mark.parametrize('denied', ['task_id', 'thread_id'])
def test_task_run_close_rechecks_live_task_and_thread_grants(tmp_path, denied):
    case = fixture(tmp_path)
    closed = close_observed_task_run(case.runtime, [case.root, case.child])
    value = case.task.task_id if denied == 'task_id' else 'background'
    actor = BusinessActor(case.owner, 'restricted', case.service, lambda _a, facts: facts.get(denied) != value)
    page = case.service.query('events.list', {'filter': {}}, actor=actor)
    assert page['events'] == [] and page['coverage'] == 'complete'
    scope = case.service.scope_digest('events.list', {'filter': {}}, actor)
    assert case.service.cursor_position(page['next_cursor'], scope)['after'] == closed['seq']


def test_foreign_owner_lifecycle_rows_are_processed_without_disclosure(tmp_path):
    case = fixture(tmp_path)
    foreign = case.runtime.record_run_creation(owner_id='other/owner', run_id='foreign', thread_id='private-thread')
    closed = close_observed_task_run(case.runtime, [foreign])
    page = case.service.query('events.list', {'filter': {}, 'limit': 100}, actor=case.actor)
    assert page['coverage'] == 'complete'
    assert event_position(case, page['next_cursor'], {}) == closed['seq']
    assert all(row['run_id'] != 'foreign' for row in page['events'])
    assert closed['event_id'] not in {row['event_id'] for row in page['events']}
    assert 'private-thread' not in str(page)


@pytest.mark.parametrize('corruption', ['missing-task-run', 'cross-task-run', 'missing-agent', 'missing-attempt'])
def test_real_missing_or_conflicting_references_remain_partial(tmp_path, corruption):
    case = fixture(tmp_path)
    first = case.service.query('events.list', {'filter': {}}, actor=case.actor)
    later = case.runtime.record_run_creation(owner_id=case.owner, run_id='another-run', thread_id='background')
    # 在规范表里构造坏引用，不修改观察实现或用替身伪造完整结果。
    fields = {'attempt_id': '', 'agent_run_id': '', 'task_run_id': 'missing-task-run'}
    if corruption == 'cross-task-run':
        fields.update(agent_run_id=case.root['agent_run_id'], task_run_id=later['task_run_id'])
    elif corruption == 'missing-agent':
        fields.update(agent_run_id='missing-agent', task_run_id=case.root['task_run_id'])
    elif corruption == 'missing-attempt':
        fields.update(agent_run_id=case.root['agent_run_id'], attempt_id='missing-attempt', task_run_id=case.root['task_run_id'])
    bad = case.runtime.append_event(event_type='lifecycle.progress', **fields)
    valid = case.runtime.append_event(event_type='attempt.progress', attempt_id=case.root['attempt_id'],
        agent_run_id=case.root['agent_run_id'], task_run_id=case.root['task_run_id'])
    page = case.service.query('events.list', {'filter': {}, 'cursor': first['next_cursor']}, actor=case.actor)
    assert page['coverage'] == 'partial'
    assert not {bad['event_id'], valid['event_id']} & {row['event_id'] for row in page['events']}
    assert event_position(case, page['next_cursor'], {}) == bad['seq'] - 1
    again = case.service.query('events.list', {'filter': {}, 'cursor': page['next_cursor']}, actor=case.actor)
    assert again['events'] == [] and again['coverage'] == 'partial'
    assert event_position(case, again['next_cursor'], {}) == bad['seq'] - 1


def test_invalid_frozen_business_linkage_does_not_become_complete(tmp_path):
    import json

    case = fixture(tmp_path)
    first = case.service.query('events.list', {'filter': {}}, actor=case.actor)
    closed = close_observed_task_run(case.runtime, [case.root, case.child])
    with case.runtime.transaction() as conn:
        metadata = {'business_context': case.task.metadata['business_context'], 'session_task_ref': 'not-this-task'}
        conn.execute('UPDATE task_runs SET metadata_json=? WHERE task_run_id=?',
            (json.dumps(metadata), case.root['task_run_id']))
    page = case.service.query('events.list', {'filter': {}, 'cursor': first['next_cursor']}, actor=case.actor)
    assert page['coverage'] == 'partial'
    assert closed['event_id'] not in {row['event_id'] for row in page['events']}
    assert event_position(case, page['next_cursor'], {}) < closed['seq']


@pytest.mark.parametrize('explicit', [False, True])
def test_close_event_keeps_its_terminal_fact_after_same_task_run_reopens(tmp_path, explicit):
    case = fixture(tmp_path)
    closed = close_observed_task_run(case.runtime, [case.root, case.child], explicit=explicit)
    before = case.service.query('events.list', {'filter': {}}, actor=case.actor)
    [original] = [row for row in before['events'] if row['event_id'] == closed['event_id']]
    assert original['payload']['revision'] == int(closed['created_at'] * 1000000)
    attempt = case.runtime.create_attempt(case.root['agent_run_id'])
    try:
        page = case.service.query('events.list', {'filter': {}}, actor=case.actor)
        [close] = [row for row in page['events'] if row['event_id'] == closed['event_id']]
        assert close['payload']['status'] == 'done'
        assert close['payload'] == original['payload']
        assert close['node'] is None and close['run_id'] is None and close['attempt_id'] is None
        assert any(row['event_type'] == 'task_run.reopened' and row['attempt_id'] == attempt['attempt_id']
            for row in page['events'])
    finally:
        case.runtime.settle_agent_run(agent_run_id=case.root['agent_run_id'],
            attempt_id=attempt['attempt_id'], status='done')


@pytest.mark.parametrize('replacement', [False, True])
def test_event_cursor_expires_after_retention_or_source_replacement(tmp_path, replacement):
    case = fixture(tmp_path)
    page = case.service.query('events.list', {'filter': {}, 'limit': 1}, actor=case.actor)
    if replacement:
        old = case.runtime.db_path.with_suffix('.old')
        case.runtime.db_path.rename(old)
        case.runtime.db_path.write_bytes(old.read_bytes())
    else:
        close_observed_task_run(case.runtime, [case.root, case.child])
        with case.runtime.transaction() as conn:
            conn.execute('DELETE FROM runtime_events WHERE seq<=4')
    with pytest.raises(BusinessError, match='CURSOR_EXPIRED'):
        case.service.query('events.list', {'filter': {}, 'cursor': page['next_cursor']}, actor=case.actor)


def test_existing_attempt_from_another_same_named_agent_stays_partial(tmp_path):
    case = fixture(tmp_path)
    other = case.runtime.record_run_creation(owner_id=case.owner, run_id='root-run', thread_id='background',
        task_run_metadata={'business_context': case.task.metadata['business_context'], 'session_task_ref': case.task.task_id})
    first = case.service.query('events.list', {'filter': {}}, actor=case.actor)
    bad = case.runtime.append_event(event_type='attempt.progress', attempt_id=other['attempt_id'],
        agent_run_id=case.root['agent_run_id'], task_run_id=case.root['task_run_id'])
    page = case.service.query('events.list', {'filter': {}, 'cursor': first['next_cursor']}, actor=case.actor)
    assert page['events'] == [] and page['coverage'] == 'partial'
    assert event_position(case, page['next_cursor'], {}) == bad['seq'] - 1


@pytest.mark.parametrize('conflicting', [False, True])
def test_close_branch_checks_nonempty_session_task_run_binding(tmp_path, conflicting):
    case = fixture(tmp_path)
    other = case.runtime.record_run_creation(owner_id=case.owner, run_id='binding-run', thread_id='background',
        task_run_metadata={'business_context': case.task.metadata['business_context'], 'session_task_ref': case.task.task_id})
    case.store.session_tasks.bind_execution(case.task.task_id, target_thread_id='background',
        task_run_id=case.root['task_run_id'] if conflicting else other['task_run_id'])
    assert case.runtime.settle_agent_run(agent_run_id=other['agent_run_id'],
        attempt_id=other['attempt_id'], status='done')['settled']
    before = case.service.query('events.list', {'filter': {}}, actor=case.actor)
    assert case.runtime.settle_task_run_if_agent_tree_terminal(task_run_id=other['task_run_id'])['settled']
    page = case.service.query('events.list', {'filter': {}, 'cursor': before['next_cursor']}, actor=case.actor)
    if conflicting:
        assert page['events'] == [] and page['coverage'] == 'partial'
        assert event_position(case, page['next_cursor'], {}) == event_position(case, before['next_cursor'], {})
    else:
        assert [row['event_type'] for row in page['events']] == ['task_run.closed']
        assert page['coverage'] == 'complete'


def test_foreign_owner_bad_reference_cannot_block_own_following_event(tmp_path):
    case = fixture(tmp_path)
    foreign_a = case.runtime.record_run_creation(owner_id='other/owner', run_id='foreign-a', thread_id='private-a')
    foreign_b = case.runtime.record_run_creation(owner_id='other/owner', run_id='foreign-b', thread_id='private-b')
    first = case.service.query('events.list', {'filter': {}}, actor=case.actor)
    case.runtime.append_event(event_type='attempt.progress', attempt_id=foreign_a['attempt_id'],
        agent_run_id=foreign_a['agent_run_id'], task_run_id=foreign_b['task_run_id'])
    own = case.runtime.append_event(event_type='attempt.progress', attempt_id=case.root['attempt_id'],
        agent_run_id=case.root['agent_run_id'], task_run_id=case.root['task_run_id'])
    page = case.service.query('events.list', {'filter': {}, 'cursor': first['next_cursor']}, actor=case.actor)
    assert [row['event_id'] for row in page['events']] == [own['event_id']]
    assert page['coverage'] == 'complete'
    assert event_position(case, page['next_cursor'], {}) == own['seq']
    assert 'private-a' not in str(page) and 'private-b' not in str(page)


@pytest.mark.parametrize('empty', [False, True])
def test_zero_position_cursor_detects_retention_but_new_snapshot_can_start(tmp_path, empty):
    case = fixture(tmp_path)
    cursor = case.service.initial_event_cursor({}, actor=case.actor)
    closed = close_observed_task_run(case.runtime, [case.root, case.child])
    with case.runtime.transaction() as conn:
        conn.execute('DELETE FROM runtime_events WHERE seq<=?', (closed['seq'] if empty else 4,))
    with pytest.raises(BusinessError, match='CURSOR_EXPIRED'):
        case.service.query('events.list', {'filter': {}, 'cursor': cursor}, actor=case.actor)
    fresh = case.service.initial_event_cursor({}, actor=case.actor)
    page = case.service.query('events.list', {'filter': {}, 'cursor': fresh}, actor=case.actor)
    assert [row['event_type'] for row in page['events']] == ([] if empty else [
        'agent_run.completed', 'agent_run.completed', 'task_run.closed'])
    assert page['coverage'] == 'complete'
    again = case.service.query('events.list', {'filter': {}, 'cursor': page['next_cursor']}, actor=case.actor)
    assert again['events'] == [] and again['coverage'] == 'complete'
    later = case.runtime.append_event(event_type='attempt.progress', attempt_id=case.root['attempt_id'],
        agent_run_id=case.root['agent_run_id'], task_run_id=case.root['task_run_id'])
    resumed = case.service.query('events.list', {'filter': {}, 'cursor': again['next_cursor']}, actor=case.actor)
    assert [row['event_id'] for row in resumed['events']] == [later['event_id']]
    assert event_position(case, resumed['next_cursor'], {}) == later['seq']


@pytest.mark.parametrize('payload', [{}, {'status': 'created'}, {'status': ['done']}, ['done']])
def test_invalid_close_status_is_an_explicit_partial_gap(tmp_path, payload):
    case = fixture(tmp_path)
    first = case.service.query('events.list', {'filter': {}}, actor=case.actor)
    bad = case.runtime.append_event(event_type='task_run.closed', attempt_id='', agent_run_id='',
        task_run_id=case.root['task_run_id'], payload=payload)
    page = case.service.query('events.list', {'filter': {}, 'cursor': first['next_cursor']}, actor=case.actor)
    assert page['events'] == [] and page['coverage'] == 'partial'
    assert event_position(case, page['next_cursor'], {}) == bad['seq'] - 1


def test_own_attempt_cannot_be_hidden_by_conflicting_foreign_records(tmp_path):
    case = fixture(tmp_path)
    foreign = case.runtime.record_run_creation(owner_id='other/owner', run_id='foreign', thread_id='private-thread')
    first = case.service.query('events.list', {'filter': {}}, actor=case.actor)
    bad = case.runtime.append_event(event_type='attempt.progress', attempt_id=case.root['attempt_id'],
        agent_run_id=foreign['agent_run_id'], task_run_id=foreign['task_run_id'])
    case.runtime.append_event(event_type='attempt.progress', attempt_id=case.root['attempt_id'],
        agent_run_id=case.root['agent_run_id'], task_run_id=case.root['task_run_id'])
    page = case.service.query('events.list', {'filter': {}, 'cursor': first['next_cursor']}, actor=case.actor)
    assert page['events'] == [] and page['coverage'] == 'partial'
    assert event_position(case, page['next_cursor'], {}) == bad['seq'] - 1
    assert 'private-thread' not in str(page)


@pytest.mark.parametrize('late_owner', ['foreign', 'own'])
def test_foreign_event_waits_for_nonempty_missing_attempt(tmp_path, late_owner):
    case = fixture(tmp_path)
    foreign = case.runtime.record_run_creation(owner_id='other/owner', run_id='foreign', thread_id='private-thread')
    first = case.service.query('events.list', {'filter': {}}, actor=case.actor)
    bad = case.runtime.append_event(event_type='attempt.progress', attempt_id='late-attempt',
        agent_run_id=foreign['agent_run_id'], task_run_id=foreign['task_run_id'])
    own = case.runtime.append_event(event_type='attempt.progress', attempt_id=case.root['attempt_id'],
        agent_run_id=case.root['agent_run_id'], task_run_id=case.root['task_run_id'])
    page = case.service.query('events.list', {'filter': {}, 'cursor': first['next_cursor']}, actor=case.actor)
    assert page['events'] == [] and page['coverage'] == 'partial'
    assert event_position(case, page['next_cursor'], {}) == bad['seq'] - 1
    target = foreign if late_owner == 'foreign' else case.root
    with case.runtime.transaction() as conn:
        conn.execute('INSERT INTO agent_attempts(attempt_id,agent_run_id,attempt_generation,status,started_at) '
            'VALUES (?,?,?,?,?)', ('late-attempt', target['agent_run_id'], 2, 'pending', 100))
    resumed = case.service.query('events.list', {'filter': {}, 'cursor': page['next_cursor']}, actor=case.actor)
    if late_owner == 'foreign':
        assert [row['event_id'] for row in resumed['events']] == [own['event_id']]
        assert resumed['coverage'] == 'complete'
        assert event_position(case, resumed['next_cursor'], {}) == own['seq']
    else:
        assert resumed['events'] == [] and resumed['coverage'] == 'partial'
        assert event_position(case, resumed['next_cursor'], {}) == bad['seq'] - 1
    assert 'private-thread' not in str(page) + str(resumed)


def test_foreign_task_does_not_skip_missing_explicit_agent(tmp_path):
    case = fixture(tmp_path)
    foreign = case.runtime.record_run_creation(owner_id='other/owner', run_id='foreign', thread_id='private-thread')
    first = case.service.query('events.list', {'filter': {}}, actor=case.actor)
    bad = case.runtime.append_event(event_type='agent_run.progress', attempt_id='',
        agent_run_id='late-agent', task_run_id=foreign['task_run_id'])
    case.runtime.append_event(event_type='attempt.progress', attempt_id=case.root['attempt_id'],
        agent_run_id=case.root['agent_run_id'], task_run_id=case.root['task_run_id'])
    page = case.service.query('events.list', {'filter': {}, 'cursor': first['next_cursor']}, actor=case.actor)
    assert page['events'] == [] and page['coverage'] == 'partial'
    assert event_position(case, page['next_cursor'], {}) == bad['seq'] - 1
    assert 'private-thread' not in str(page)
