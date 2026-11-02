"""原runtime_events增量恢复、NDJSON与live撤权；不运行模型或消费wake。"""
import json
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.conversation.business_access import BusinessError
from agent_py_agent.agent.gateway_parts.business_event_stream import (
    BUSINESS_STREAM_MAX_PENDING_EVENTS,
    stream_business_events,
)
from agent_py_agent.tests.test_business_v12_f7_observation import fixture, subscription_fixture
from agent_py_agent.tests.test_mega_business_api_actions import (
    background_http as background_http,
)
from agent_py_agent.tests.test_plugin_host_gateway_composition import (
    production_host as production_host,
)


def append(case, *, attempt=None):
    return case.runtime.append_event(event_type='attempt.progress', attempt_id=attempt or case.root['attempt_id'],
        agent_run_id=case.root['agent_run_id'], task_run_id=case.root['task_run_id'], payload={'secret': 'not-a-public-payload'})


def stream(case, cursor=None):
    request = {'request_id': 'request-1', 'filter': {'space_id': 'space-a'}}
    if cursor is not None:
        request['cursor'] = cursor
    result = stream_business_events(request, authenticated_context=case.actor,
        services=SimpleNamespace(observation=case.service, wait=lambda _seconds: None))
    assert result is not None, 'stream入口必须返回真实可迭代响应'
    return result


def test_real_source_events_reconnect_by_id_without_consuming_work(tmp_path):
    case = fixture(tmp_path)
    event = append(case)
    try:
        first = case.service.query('events.list', {'filter': {'space_id': 'space-a'}}, actor=case.actor)
    except BusinessError as error:
        first = {'error': error.code}
    assert 'error' not in first, first
    assert event['event_id'] in {row['event_id'] for row in first['events']}
    second = case.service.query('events.list', {'filter': {'space_id': 'space-a'}, 'cursor': first['next_cursor']}, actor=case.actor)
    assert second['events'] == []
    new = append(case)
    third = case.service.query('events.list', {'filter': {'space_id': 'space-a'}, 'cursor': second['next_cursor']}, actor=case.actor)
    assert [row['event_id'] for row in third['events']] == [new['event_id']]
    assert case.store.session_tasks.require(case.task.task_id).status == 'queued'
    assert 'not-a-public-payload' not in str(first)


def test_ndjson_snapshot_heartbeat_and_revocation_are_readonly(tmp_path):
    case = fixture(tmp_path)
    response = stream(case)
    assert response.content_type == 'application/x-ndjson'
    iterator = iter(response)
    snapshot = json.loads(next(iterator))
    assert snapshot['stream_item'] == 'snapshot'
    assert snapshot['payload']['tasks'][0]['task_id'] == case.task.task_id
    before = case.runtime.db_path.read_bytes()
    for _ in range(30):
        item = json.loads(next(iterator))
        if item['stream_item'] == 'heartbeat':
            break
    else:
        pytest.fail('空来源必须产生heartbeat而不是启动业务')
    assert case.runtime.db_path.read_bytes() == before
    case.allowed[0] = False
    with pytest.raises(StopIteration):
        next(iterator)
    assert case.store.session_tasks.require(case.task.task_id).status == 'queued'


def test_slow_client_burst_gets_gap_and_never_unbounded_queue(tmp_path):
    case = fixture(tmp_path)
    response = stream(case)
    iterator = response.events()
    snapshot = next(iterator)
    assert snapshot['stream_item'] == 'snapshot'
    for _ in range(BUSINESS_STREAM_MAX_PENDING_EVENTS + 2):
        append(case)
    gap = next(iterator)
    assert gap['stream_item'] == 'gap' and gap['payload']['reset_required'] is True
    assert response.pending_count <= BUSINESS_STREAM_MAX_PENDING_EVENTS
    with pytest.raises(StopIteration):
        next(iterator)


def test_expired_and_changed_scope_cursor_cannot_resume_old_authority(tmp_path):
    case = fixture(tmp_path)
    first = next(stream(case).events())
    case.service._cursor_key = b'y' * 32
    expired = next(stream(case, first['cursor']).events())
    assert expired['stream_item'] == 'cursor_expired' and expired['coverage'] == 'expired'


def test_late_attempt_does_not_get_skipped_by_cursor(tmp_path):
    case = fixture(tmp_path)
    late = append(case, attempt='late-attempt')
    try:
        page = case.service.query('events.list', {'filter': {'space_id': 'space-a'}}, actor=case.actor)
    except BusinessError as error:
        page = {'error': error.code}
    assert 'error' not in page
    assert page['coverage'] == 'partial'
    assert late['event_id'] not in {row['event_id'] for row in page['events']}
    with case.runtime.transaction() as conn:
        conn.execute('INSERT INTO agent_attempts(attempt_id,agent_run_id,attempt_generation,status,started_at) VALUES (?,?,?,?,?)',
            ('late-attempt', case.root['agent_run_id'], 2, 'pending', 100))
    resumed = case.service.query('events.list', {'filter': {'space_id': 'space-a'}, 'cursor': page['next_cursor']}, actor=case.actor)
    assert late['event_id'] in {row['event_id'] for row in resumed['events']}


def receiver(case, tmp_path):
    import hashlib
    import secrets

    from agent_py_agent.agent.common.json_io import write_private_json_file_atomic

    subscription = case.subscriptions.execute('subscriptions.create', {
        'space_id': case.space, 'source_ref': 'comments', 'routing_instruction': '原路由'},
        actor=case.subscription_actor, idempotency_key='source-sub')['subscription']
    secret = secrets.token_urlsafe(32)
    token = {'token_id': 'source-token', 'token_sha256': hashlib.sha256(secret.encode()).hexdigest(),
        'enabled': True, 'expires_at': None, 'allowed_target_owners': [case.owner], 'allowed_actions': ['push_event'],
        'allowed_source_refs': ['comments'], 'allowed_subscription_ids': [subscription['subscription_id']]}
    root = tmp_path / 'app'
    token_path = root / 'remote_push' / 'tokens.json'
    write_private_json_file_atomic(token_path, {'tokens': [token]})
    server = SimpleNamespace(agent=SimpleNamespace(home_paths=SimpleNamespace(root=root)),
        business_subscriptions=lambda owner: case.subscriptions if owner == case.owner else None,
        business_subscription_actor=lambda owner, principal, source: case.subscription_actor)
    body = {'target_owner': case.owner, 'instruction': '评论正文', 'idempotency_key': 'delivery-1', 'event_id': 'comment-1',
        'subscription_id': subscription['subscription_id'], 'source_ref': 'comments', 'event_revision': 1}
    return server, body, secret, token_path


def receive(server, body, secret):
    import hashlib
    import hmac
    import time
    import uuid

    from agent_py_agent.agent.gateway_parts.remote_push_service import accept_remote_push

    raw = json.dumps(body, ensure_ascii=False, separators=(',', ':')).encode()
    stamp, nonce = str(int(time.time())), uuid.uuid4().hex
    signature = hmac.digest(secret.encode(), f'{stamp}.{nonce}.{hashlib.sha256(raw).hexdigest()}'.encode(), 'sha256').hex()
    return accept_remote_push(server, raw, {'X-Agent-Push-Token': secret, 'X-Agent-Timestamp': stamp,
        'X-Agent-Nonce': nonce, 'X-Agent-Signature': signature}, '127.0.0.1')


def test_signed_subscription_ingress_reuses_original_receiving_ledger(tmp_path):
    from agent_py_agent.agent.gateway_parts.remote_push_service import RemotePushError
    case = subscription_fixture(tmp_path / 'owner')
    server, body, secret, token_path = receiver(case, tmp_path)
    try:
        first = receive(server, body, secret)
    except RemotePushError as error:
        first = {'error_code': error.error_code}
    assert 'error_code' not in first, first
    duplicate = receive(server, {**body, 'idempotency_key': 'delivery-2'}, secret)
    assert first['task_id'] == duplicate['task_id'] and duplicate['duplicate'] is True
    edited = receive(server, {**body, 'event_revision': 2, 'idempotency_key': 'delivery-3'}, secret)
    assert edited['task_id'] != first['task_id']
    tasks, errors = case.store.session_tasks.list_report(limit=0)
    assert not errors and len(tasks) == 3 and all(task.status == 'queued' for task in tasks)
    ledger = json.loads(token_path.with_name('receipts.json').read_text())
    assert len(ledger['entries']) == 2
    assert not (tmp_path / 'owner' / 'data' / 'source_events').exists()


def test_unknown_ack_replays_frozen_instruction_after_routing_edit(tmp_path):
    from agent_py_agent.agent.gateway_parts.remote_push_service import RemotePushError
    case = subscription_fixture(tmp_path / 'owner')
    server, body, secret, _path = receiver(case, tmp_path)
    original = case.subscriptions.background_service.submit

    def lose_ack(request, *, actor, idempotency_key):
        original(request, actor=actor, idempotency_key=idempotency_key)
        raise OSError('fault after original canonical task commit')

    case.subscriptions.background_service.submit = lose_ack
    try:
        receive(server, body, secret)
    except (RemotePushError, OSError) as error:
        observed = getattr(error, 'error_code', 'ACK_LOST')
    else:
        observed = 'NO_FAULT'
    assert observed == 'ACK_LOST', observed
    case.subscriptions.execute('subscriptions.update', {'subscription_id': body['subscription_id'], 'routing_instruction': '后改路由'},
        actor=case.subscription_actor, expected_revision=0, idempotency_key='edit-route')
    case.subscriptions.background_service.submit = original
    receipt = receive(server, {**body, 'idempotency_key': 'retry-delivery'}, secret)
    tasks, errors = case.store.session_tasks.list_report(limit=0)
    assert not errors and len(tasks) == 2
    assert receipt['task_id'] != case.task.task_id
    assert '原路由' in case.store.session_tasks.require(receipt['task_id']).goal
    assert '后改路由' not in case.store.session_tasks.require(receipt['task_id']).goal


def test_managed_push_missing_source_acl_and_reused_revision_fail_closed(tmp_path):
    from agent_py_agent.agent.common.json_io import write_private_json_file_atomic
    from agent_py_agent.agent.gateway_parts.remote_push_service import RemotePushError
    case = subscription_fixture(tmp_path / 'owner')
    server, body, secret, path = receiver(case, tmp_path)
    config = json.loads(path.read_text())
    config['tokens'][0].pop('allowed_source_refs')
    write_private_json_file_atomic(path, config)
    try:
        receive(server, body, secret)
    except RemotePushError as error:
        code = error.error_code
    else:
        code = 'UNEXPECTED_ACCEPTANCE'
    assert code == 'REMOTE_PUSH_TOKEN_SCOPE_DENIED', code


def test_agent_filter_stream_still_delivers_its_queued_event(tmp_path):
    case = fixture(tmp_path)
    response = stream_business_events({'request_id': 'by-agent', 'filter': {'space_id': 'space-a', 'agent_id': 'background'}},
        authenticated_context=case.actor, services=SimpleNamespace(observation=case.service, wait=lambda _s: None))
    iterator = response.events()
    assert next(iterator)['stream_item'] == 'snapshot'
    event = append(case)
    seen = []
    try:
        for _ in range(10):
            item = next(iterator)
            if item['stream_item'] == 'event':
                seen.append(item['event_id'])
            if item['stream_item'] == 'heartbeat':
                break
    except StopIteration:
        pass
    assert event['event_id'] in seen


@pytest.mark.parametrize('extra', [{'project_ref': 'not-this-project'}, {'agent_id': 'not-this-agent'}, {'attempt_id': 'not-this-attempt'}])
def test_snapshot_intersects_every_filter_not_just_space(tmp_path, extra):
    case = fixture(tmp_path)
    request = {'request_id': 'narrow-snapshot', 'filter': {'space_id': 'space-a', **extra}}
    response = stream_business_events(request, authenticated_context=case.actor,
        services=SimpleNamespace(observation=case.service, wait=lambda _s: None))
    frame = next(response.events())
    assert frame['payload']['nodes'] == []
    assert frame['payload']['tasks'] == [] and frame['payload']['executions'] == []


def test_plugin_domain_queue_is_eight_and_disconnect_cannot_cancel_task(tmp_path):
    case = fixture(tmp_path)
    response = stream_business_events({'request_id': 'plugin', 'filter': {'space_id': 'space-a'}},
        authenticated_context=case.actor,
        services=SimpleNamespace(observation=case.service, stream_transport='plugin', wait=lambda _s: None))
    assert response.max_pending == 8
    iterator = response.events()
    assert next(iterator)['stream_item'] == 'snapshot'
    for _ in range(9):
        append(case)
    assert next(iterator)['stream_item'] == 'gap'
    iterator.close()
    assert response.pending_count == 0
    assert case.store.session_tasks.require(case.task.task_id).status == 'queued'


def test_event_time_filter_uses_event_timestamp_not_execution_start(tmp_path):
    from datetime import datetime, timezone
    case = fixture(tmp_path)
    event = append(case)
    since = datetime.fromtimestamp(event['created_at'], timezone.utc).isoformat()
    page = case.service.query('events.list', {'filter': {'space_id': 'space-a', 'since': since}}, actor=case.actor)
    assert event['event_id'] in {row['event_id'] for row in page['events']}


def test_disappearing_source_produces_explicit_expiry_not_silent_hang(tmp_path):
    case = fixture(tmp_path)
    response = stream(case)
    iterator = response.events()
    assert next(iterator)['stream_item'] == 'snapshot'
    case.runtime.db_path.rename(tmp_path / 'old-runtime.db')
    try:
        value = next(iterator)
    except OSError:
        value = {'error': 'unclassified_source_loss'}
    assert value.get('stream_item') == 'cursor_expired', value
    assert value['payload']['reset_required'] is True


def test_native_real_stream_missing_db_live_disable_and_disconnect(background_http):
    from agent_py_agent.agent.runtime_db.schema import runtime_db_path
    from agent_py_agent.tests.test_mega_business_api_actions import (
        observed_native_task,
        open_native_observation,
    )

    services, task = observed_native_task(background_http)
    db = runtime_db_path(services.owner_home)
    assert not db.exists()
    conn, response = open_native_observation(background_http, {'request_id': 'native-live-disable', 'filter': {}})
    try:
        assert response.status == 200, response.read()
        snapshot = json.loads(response.readline())
        assert snapshot['stream_item'] == 'snapshot'
        assert snapshot['payload']['tasks'][0]['task_id'] == task.task_id
        background_http[1].config.business_spaces_enabled = False
        assert response.readline() == b'', '下一次真实流复核后必须停止交付'
    finally:
        response.close()
        conn.close()
    assert not db.exists()
    assert services.host.conversation_store.session_tasks.require(task.task_id).status == task.status
    conn, response = open_native_observation(background_http, {'request_id': 'native-still-disabled', 'filter': {}})
    try:
        assert response.status == 503 and json.loads(response.read())['error']['code'] == 'HOST_NOT_READY'
    finally:
        response.close()
        conn.close()


def test_plugin_real_sse_live_issuer_revocation_and_missing_db(production_host):
    from agent_py_agent.agent.runtime_db.schema import runtime_db_path
    from agent_py_agent.tests.test_mega_business_plugin_compatibility import (
        open_plugin_observation,
        plugin_observation_task,
        read_business_sse,
    )

    services, task = plugin_observation_task(production_host)
    db = runtime_db_path(services.owner_home)
    assert not db.exists()
    conn, response = open_plugin_observation(production_host)
    try:
        assert response.status == 200, response.read()
        kind, first = read_business_sse(response)
        assert kind == 'business' and first['stream_item'] == 'snapshot'
        production_host[0].plugin_host.composition.issuer.close()
        kind, final = read_business_sse(response)
        assert kind == 'error' and final['outcome'] == 'not_started', final
        assert '/private/tmp' not in str(final) and 'Traceback' not in str(final)
        assert response.readline() == b'', '原issuer撤权后SSE必须结束'
    finally:
        response.close()
        conn.close()
    assert not db.exists()
    assert services.host.conversation_store.session_tasks.require(task.task_id).status == task.status


def test_plugin_domain_closes_the_real_typed_iterator_and_retains_task(production_host):
    from agent_py_agent.agent.plugin_host import composition
    from agent_py_agent.agent.plugin_host.contracts import decode_call
    from agent_py_agent.tests.test_mega_business_api_actions import seed_observed_runtime
    from agent_py_agent.tests.test_mega_business_plugin_compatibility import plugin_observation_task

    services, task = plugin_observation_task(production_host)
    runtime, run = seed_observed_runtime(services, task)
    for _ in range(3):
        runtime.append_event(event_type='attempt.progress', attempt_id=run['attempt_id'],
            agent_run_id=run['agent_run_id'], task_run_id=run['task_run_id'])
    server, token, *_ = production_host
    issuer = server.plugin_host.composition.issuer
    authorized = issuer.authorize(issuer.ingress(token), decode_call(json.dumps({'schema': 'plugin_host_call.v2',
        'action': 'business.events.stream', 'payload': {'request_id': 'typed-close', 'filter': {}}}).encode()))
    backend = composition.business_port(server.plugin_host.composition.ports)
    iterator = backend.stream(authorized)
    assert next(iterator)['stream_item'] == 'snapshot'
    assert next(iterator)['stream_item'] == 'event'
    typed = iterator.gi_frame.f_locals['iterator']
    source = typed.gi_frame.f_locals['self']
    assert source.max_pending == 8 and source.pending_count > 0
    iterator.close()
    assert typed.gi_frame is None and source.pending_count == 0
    assert services.host.conversation_store.session_tasks.require(task.task_id).status == task.status


@pytest.mark.parametrize('transport', ['native', 'plugin'])
@pytest.mark.parametrize('explicit', [False, True])
def test_typed_stream_task_run_close_and_resume(tmp_path, transport, explicit):
    from agent_py_agent.tests.test_business_v12_f7_observation import (
        close_observed_task_run,
        event_position,
    )

    case = fixture(tmp_path)
    response = stream_business_events({'request_id': 'lifecycle', 'filter': {'space_id': 'space-a'}},
        authenticated_context=case.actor, services=SimpleNamespace(observation=case.service,
            stream_transport=transport, wait=lambda _s: None))
    iterator = response.events()
    assert next(iterator)['stream_item'] == 'snapshot'
    # 先读完原创建事件，避免把合法的关闭回归误做成超过 Plugin 八项的背压测试。
    for _ in range(4):
        assert next(iterator)['stream_item'] == 'event'
    assert next(iterator)['stream_item'] == 'heartbeat'
    closed = close_observed_task_run(case.runtime, [case.root, case.child], explicit=explicit)
    later = case.runtime.record_run_creation(owner_id=case.owner, run_id='after-close', thread_id='background',
        task_run_metadata={'business_context': case.task.metadata['business_context'], 'session_task_ref': case.task.task_id})
    frames = [next(iterator) for _ in range(5)]
    assert [row['event_type'] for row in frames] == [
        'agent_run.completed', 'agent_run.completed', 'task_run.closed', 'task.created', 'agent_run.created']
    assert frames[2]['event_id'] == closed['event_id'] and frames[2]['attempt_id'] is None
    assert frames[-1]['attempt_id'] == later['attempt_id']
    positions = [event_position(case, row['cursor'], {'space_id': 'space-a'}) for row in frames]
    assert positions == sorted(set(positions))
    iterator.close()
    assert response.pending_count == 0
    resumed = stream(case, frames[2]['cursor']).events()
    assert next(resumed)['stream_item'] == 'snapshot'
    assert [next(resumed)['event_id'] for _ in range(2)] == [row['event_id'] for row in frames[3:]]
    assert next(resumed)['stream_item'] == 'heartbeat'
    resumed.close()


@pytest.mark.parametrize('transport', ['native', 'plugin'])
@pytest.mark.parametrize('explicit', [False, True])
def test_real_transports_deliver_task_run_close_and_resume(request, transport, explicit):
    from agent_py_agent.tests.test_business_v12_f7_observation import (
        close_observed_task_run,
    )
    from agent_py_agent.tests.test_mega_business_api_actions import (
        background_call,
        observed_native_task,
        open_native_observation,
        seed_observed_runtime,
    )
    from agent_py_agent.tests.test_mega_business_plugin_compatibility import (
        open_plugin_observation,
        plugin_observation_task,
        production_call,
        read_business_sse,
    )

    host = request.getfixturevalue('background_http' if transport == 'native' else 'production_host')
    services, task = observed_native_task(host) if transport == 'native' else plugin_observation_task(host)
    runtime, run = seed_observed_runtime(services, task)
    if transport == 'native':
        status, result = background_call(host, 'events.list', {'filter': {}})
        assert status == 200, result
        cursor = result['data']['next_cursor']
    else:
        cursor = production_call(host, 'business.events.list', {'filter': {}})['next_cursor']
    closed = close_observed_task_run(runtime, [run], explicit=explicit)
    later = runtime.record_run_creation(owner_id=services.owner_id, run_id='wire-after-close',
        thread_id=task.target_thread_id, task_run_metadata={
            'business_context': task.metadata['business_context'], 'session_task_ref': task.task_id})

    def connect(selected_cursor):
        if transport == 'native':
            return open_native_observation(host, {'request_id': 'wire-close', 'filter': {}, 'cursor': selected_cursor})
        return open_plugin_observation(host, cursor=selected_cursor)

    def read_frame(response):
        if transport == 'native':
            return json.loads(response.readline())
        kind, frame = read_business_sse(response)
        assert kind == 'business', frame
        return frame

    conn, response = connect(cursor)
    try:
        assert response.status == 200, response.read()
        assert read_frame(response)['stream_item'] == 'snapshot'
        frames = [read_frame(response) for _ in range(4)]
        assert [row['event_type'] for row in frames] == [
            'agent_run.completed', 'task_run.closed', 'task.created', 'agent_run.created']
        assert frames[1]['event_id'] == closed['event_id']
        assert frames[1]['node'] is None and frames[1]['attempt_id'] is None
        assert frames[-1]['attempt_id'] == later['attempt_id']
        assert 'private-close-reason' not in str(frames)
    finally:
        response.close()
        conn.close()
    conn, response = connect(frames[1]['cursor'])
    try:
        assert response.status == 200, response.read()
        assert read_frame(response)['stream_item'] == 'snapshot'
        assert [read_frame(response)['event_id'] for _ in range(2)] == [row['event_id'] for row in frames[2:]]
    finally:
        response.close()
        conn.close()
    assert services.host.conversation_store.session_tasks.require(task.task_id).status == task.status
