"""issue70真实Gateway往返：seed规范来源，不手写前端响应或替换授权判定。"""
from __future__ import annotations

import hashlib
import json
import time
from dataclasses import replace
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.conversation.background_team_lifecycle import background_identity
from agent_py_agent.agent.conversation.business_access import BusinessActor, BusinessError
from agent_py_agent.agent.conversation.business_contracts import (
    BusinessContextIntent,
    BusinessExecutionContext,
)
from agent_py_agent.agent.conversation.business_observability import BusinessObservationService
from agent_py_agent.agent.conversation.display_archive import archive_display_rows
from agent_py_agent.agent.conversation.session_tasks import SessionTaskDraft
from agent_py_agent.agent.conversation.store import ConversationStore
from agent_py_agent.agent.gateway_parts import business_api as api
from agent_py_agent.agent.gateway_parts.business_api_v13_readers import (
    default_observation_readers,
    native_approval_readers,
)
from agent_py_agent.agent.observability import concurrency_metrics
from agent_py_agent.agent.runtime_db.repository import RuntimeRepository
from agent_py_agent.tests import test_mega_business_api_actions as business_actions
from agent_py_agent.tests.test_mega_business_api_actions import (
    background_call,
    create_background,
    seed_observed_runtime,
)

# 绑定原夹具对象，保留 pytest 发现语义，避免导入名与测试参数触发重定义。
background_http = business_actions.background_http


# LLM: 所有测试沿任务tmp home，不读取真实运行环境。
# 函数用途: 让fixture创建的原生Gateway始终使用隔离HOME。
@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    monkeypatch.setenv('HOME', str(tmp_path))
    monkeypatch.setenv('MY_AGENT_HOME', str(tmp_path / 'home'))


# LLM: 观察夹具只seed原canonical任务，不测试派发/wake，也不触碰真实全局publication根。
# 函数用途: 经HTTP创建真实后台线程后使用原SessionTaskStore创建规范任务。
def observed_task(background, *, user='business-alice'):
    params, agent = create_background(background, user=user)
    [services] = [item for item in background[0]._business_services.values() if item.owner_id.endswith('/' + user)]
    store = services.host.conversation_store
    target = store.threads.require(agent['agent_id'])
    identity = background_identity(target)
    intent = BusinessContextIntent(services.owner_id, params['space_id'], None, None)
    task = store.session_tasks.create(SessionTaskDraft(identity.managed_by_thread_id, target.thread_id,
        '观察规范来源', dedupe_key='reader-canonical-seed'), business_context_intent=intent,
        business_context_factory=lambda tid: BusinessExecutionContext(services.owner_id, params['space_id'],
            None, tid, 1, None, (), (), 'business-context.v1'))
    return services, task


# LLM: 数据只从RuntimeRepository原写口创建，再更新精确规范行模拟原工具结算。
# 函数用途: seed两条原工具操作及一条不可变产物。
def seeded(background):
    services, task = observed_task(background)
    runtime, run = seed_observed_runtime(services, task)
    operations = []
    for name in ('read_file', 'list_files'):
        operation = runtime.create_tool_operation(agent_run_id=run['agent_run_id'],
            attempt_id=run['attempt_id'], operation_type=name)
        operations.append(operation['operation_id'])
        with runtime._runtime_connection() as conn:
            conn.execute("UPDATE tool_operations SET status='SUCCEEDED',handler_started_at=1,settled_at=2,outcome_json=? WHERE operation_id=?",
                (json.dumps({'result': {'schema_version': 'tool_execution_result.v1', 'tool': name,
                    'ok': True, 'handler_executed': True, 'effect_outcome': 'none', 'duration_ms': 1000,
                    'result_ref': 'result-' + name}}), operation['operation_id']))
            conn.commit()
    digest = hashlib.sha256(b'canonical snapshot').hexdigest()
    with runtime._runtime_connection() as conn:
        conn.execute('INSERT INTO artifact_records(artifact_record_id,attempt_id,agent_run_id,rel_path,content_digest,size,created_at) '
            'VALUES(?,?,?,?,?,?,?)', ('artifact-snapshot', run['attempt_id'], run['agent_run_id'], 'output/evidence.txt', digest, 18, 1))
        conn.commit()
    return services, task, runtime, run, operations, digest


# LLM: 只把真实factory产生的reader装入真实Handler原观察服务，不提供假结果。
# 函数用途: seed后重绑定来源以覆盖运行过程中才出现的账本。
def wire(services, runtime):
    readers = default_observation_readers(services.owner_home, services.owner_id,
        services.host.conversation_store, runtime)
    services.observation.readers = readers
    return readers


# LLM: 不存在的来源不能被观察创建，也不因空store注册虚假0费用。
# 函数用途: 核对缺源、坏库及跨home绑定都保持未安装。
def test_default_wiring_installs_only_sources_that_exist(tmp_path):
    owner = tmp_path / 'owner'
    store = ConversationStore(owner / 'conversations')
    runtime = RuntimeRepository(owner / 'runtime.db', read_only=True)
    assert default_observation_readers(owner, 'local/test', store, runtime) == {}
    assert not runtime.db_path.exists()
    assert default_observation_readers(tmp_path / 'other', 'local/test', store, runtime) == {}
    runtime.db_path.write_bytes(b'broken database')
    assert default_observation_readers(owner, 'local/test', store, runtime) == {}


# LLM: 真实HTTP发现列表必须与已安装端口一致，缺来源的合法请求不是伪造空成功。
# 函数用途: 复核端口发现、正常及完整空列表。
def test_capabilities_match_actual_readable_ports_and_missing_usage_is_unknown(background_http):
    services, task, runtime, run, operations, digest = seeded(background_http)
    readers = wire(services, runtime)
    status, found = background_call(background_http, 'system.capabilities')
    assert status == 200, found
    actions = found['data']['supported_actions']
    assert 'tool_calls.list' in actions and 'tool_calls.get' in actions
    assert 'artifacts.list' in actions and 'artifacts.get' in actions
    assert 'logs.read' not in actions and 'usage.get' not in actions
    assert ('approvals.list' in actions) == ('approvals' in services.observation.readers)
    assert {'tool_calls', 'tool_calls.get', 'artifacts', 'artifacts.get'} <= set(readers)
    status, listed = background_call(background_http, 'tool_calls.list')
    assert status == 200 and {row['operation_id'] for row in listed['data']['items']} == set(operations), listed
    status, empty = background_call(background_http, 'tool_calls.list', {'filter': {'space_id': 'unrelated'}})
    assert status == 200 and empty['data']['items'] == [] and empty['data']['coverage'] == 'complete', empty
    status, missing = background_call(background_http, 'usage.get')
    assert status == 503 and missing['error']['code'] == 'HOST_NOT_READY', missing
    actor = BusinessActor(services.owner_id, 'u', services.observation, lambda action, facts: True)
    with pytest.raises(BusinessError) as error:
        services.observation.query('usage.get', {}, actor=actor)
    assert error.value.code == 'HOST_NOT_READY'
    assert error.value.details == {'coverage': 'unknown', 'source': 'resource_usage'}


# LLM: 真实HTTP信封沿冻结合同，202不混入已完成读取。
# 函数用途: 复核分页、工具脱敏和闭合信封。
def test_envelope_fields_closed(background_http):
    services, task, runtime, run, operations, digest = seeded(background_http)
    wire(services, runtime)
    status, first = background_call(background_http, 'tool_calls.list', {'limit': 1})
    assert status == 200 and first['state'] == 'completed' and first['error'] is None, first
    assert set(first) == {'request_id', 'operation_id', 'state', 'data', 'error', 'allowed_actions', 'warnings', 'effect_outcome'}
    assert first['data']['next_cursor'] and first['data']['coverage'] == 'partial'
    status, second = background_call(background_http, 'tool_calls.list', {'limit': 1, 'cursor': first['data']['next_cursor']})
    assert status == 200 and second['data']['next_cursor'] is None, second
    assert first['data']['items'][0]['operation_id'] != second['data']['items'][0]['operation_id']
    status, detail = background_call(background_http, 'tool_calls.get', {'operation_id': operations[0]})
    assert status == 200, detail
    assert 'synthetic-secret' not in json.dumps(detail)
    assert detail['data']['tool_call']['attempt_id'] == run['attempt_id']
    assert detail['data']['tool_call']['task_id'] == task.task_id


def test_selected_runtime_tools_show_latest_page_and_exact_attempt(background_http):
    services, _, runtime, run, operations, _ = seeded(background_http)
    wire(services, runtime)
    with runtime._runtime_connection() as conn:
        run_id = conn.execute('SELECT run_id FROM agent_runs WHERE agent_run_id=?', (run['agent_run_id'],)).fetchone()[0]
    params = {'filter': {'run_id': run_id, 'attempt_id': run['attempt_id']}, 'limit': 1}
    status, first = background_call(background_http, 'tool_calls.list', params)
    assert status == 200 and [r['operation_id'] for r in first['data']['items']] == [operations[-1]], first
    status, second = background_call(background_http, 'tool_calls.list', {**params, 'cursor': first['data']['next_cursor']})
    assert status == 200 and [r['operation_id'] for r in second['data']['items']] == [operations[0]], second
    assert not second['data']['next_cursor']
    status, wrong = background_call(background_http, 'tool_calls.list', {
        'filter': {'run_id': 'foreign-run', 'attempt_id': run['attempt_id']}, 'limit': 1})
    assert status == 200 and wrong['data']['items'] == [] and not wrong['data']['next_cursor']


# LLM: 逐记录授权不相信cursor；坏来源不能悄悄变complete。
# 函数用途: 验证撤权、跨owner以及同一已安装源损坏。
def test_round_trip_matrix(background_http):
    services, task, runtime, run, operations, digest = seeded(background_http)
    wire(services, runtime)
    actor = BusinessActor(services.owner_id, 'u', services.observation, lambda action, facts: True)
    first = services.observation.query('tool_calls.list', {'limit': 1}, actor=actor)
    revoked = BusinessActor(services.owner_id, 'u', services.observation, lambda action, facts: False)
    with pytest.raises(BusinessError, match='ACCESS_DENIED'):
        services.observation.query('tool_calls.list', {'cursor': first['next_cursor']}, actor=revoked)
    bob, bob_task = observed_task(background_http, user='business-bob')
    bob_runtime, bob_run = seed_observed_runtime(bob, bob_task)
    wire(bob, bob_runtime)
    status, foreign = background_call(background_http, 'tool_calls.get', {'operation_id': operations[0]}, user='business-bob')
    assert status == 404 and foreign['error']['code'] == 'OBSERVATION_NOT_FOUND', foreign
    assert operations[0] not in json.dumps(foreign['data'])
    with runtime._runtime_connection() as conn:
        conn.execute('UPDATE tool_operations SET outcome_json=? WHERE operation_id=?', ('{broken', operations[0]))
        conn.commit()
    status, bad = background_call(background_http, 'tool_calls.list')
    assert status == 200 and bad['data']['coverage'] == 'unknown' and bad['data']['items'] == [], bad
    status, bad_detail = background_call(background_http, 'tool_calls.get', {'operation_id': operations[0]})
    assert status == 503 and bad_detail['error']['code'] == 'HOST_NOT_READY', bad_detail


# LLM: 归档由原producer生成；不得把live文件或自编前端JSON当日志。
# 函数用途: 验证原显示归档的脱敏、字节分页和游标绑定。
def test_logs_are_redacted_and_paginated_from_canonical_archive(background_http):
    services, task, runtime, run, operations, digest = seeded(background_http)
    reference = archive_display_rows(SimpleNamespace(conversation_store=services.host.conversation_store),
        thread_id=task.target_thread_id, rows=[{'kind': 'text', 'text': 'api_key=synthetic-log-secret\n' + 'x' * 80}])
    wire(services, runtime)
    source = reference['archive_id']
    status, first = background_call(background_http, 'logs.read', {'source_ref': source, 'max_bytes': 40})
    assert status == 200 and first['data']['next_cursor'], first
    assert 'synthetic-log-secret' not in json.dumps(first)
    pieces = [first['data']['chunks'][0]['text']]
    cursor = first['data']['next_cursor']
    while cursor:
        status, page = background_call(background_http, 'logs.read', {'source_ref': source, 'max_bytes': 40, 'cursor': cursor})
        assert status == 200, page
        pieces.extend(row['text'] for row in page['data']['chunks'])
        cursor = page['data']['next_cursor']
    assert ''.join(pieces).endswith('x' * 80)
    assert page['data']['coverage'] == 'complete'
    status, denied = background_call(background_http, 'logs.read', {'source_ref': 'f' * 32, 'cursor': first['data']['next_cursor']})
    assert status >= 400, denied


# LLM: version_ref来源是原账，不从宿主live路径重新计算或借latest替换。
# 函数用途: 验证不可变产物版本与不匹配版本拒绝。
def test_artifact_versions_are_exact(background_http):
    services, task, runtime, run, operations, digest = seeded(background_http)
    wire(services, runtime)
    status, listed = background_call(background_http, 'artifacts.list')
    assert status == 200 and listed['data']['items'][0]['version_ref'] == digest, listed
    status, detail = background_call(background_http, 'artifacts.get', {'artifact_id': 'artifact-snapshot', 'version_ref': digest})
    assert status == 200 and detail['data']['version_ref'] == digest, detail
    assert detail['data']['artifact']['task_id'] == task.task_id
    assert detail['data']['available'] is False
    status, stale = background_call(background_http, 'artifacts.get', {'artifact_id': 'artifact-snapshot', 'version_ref': 'wrong-version'})
    assert status == 404, stale


# LLM: 人工签票通道不因安装观察reader放宽。
# 函数用途: 真实HTTP确认通用JSON仍拒绝决定审批。
def test_approvals_decide_still_human_only(background_http):
    services, task = observed_task(background_http)
    status, result = background_call(background_http, 'approvals.decide',
        {'approval_id': 'approval', 'decision': 'approved', 'response_ticket': 'synthetic-invalid-ticket'},
        idempotency_key='human-only', expected_revision=1)
    assert status == 403 and result['error']['code'] == 'CAPABILITY_DENIED', result


# LLM: 用原模型ledger产生决策分区，指标通过原Gauge采样，不自编响应字典。
# 函数用途: 验证已报/估算分层、unknown费用与计量缺失后的拒绝。
def test_usage_uses_decision_ledger_and_owner_meter(background_http):
    from agent_py_agent.tests.test_decision_audit_controls import _decision_calls

    services, task, runtime, run, operations, digest = seeded(background_http)
    store = services.host.conversation_store
    store.model_usage.append_snapshot_once({'thread_id': task.target_thread_id, 'request_id': 'usage-request',
        'run_id': runtime.get_agent_run(run['agent_run_id'])['run_id'], 'task_id': task.task_id, 'source': 'test',
        'model_calls': _decision_calls(finished=1, timed_out=1, input_tokens=321), 'now': time.time()})
    # 无owner标签的进程全局指标即使有数值也不能泄露为当前owner计数。
    metrics = concurrency_metrics._metrics()
    with metrics.llm_inflight._lock:
        previous_global = metrics.llm_inflight._vals.get(())
    metrics.llm_inflight.set(99)
    assert 'resource_usage' not in default_observation_readers(services.owner_home, services.owner_id, store, runtime)
    metrics.llm_inflight.set(2, labels={'owner_id': services.owner_id})
    try:
        readers = wire(services, runtime)
        assert 'usage' in readers and 'resource_usage' in readers
        status, usage = background_call(background_http, 'usage.get')
        assert status == 200, usage
        assert usage['data']['actual']['input_tokens'] == 321
        assert usage['data']['actual']['cost'] is None and usage['data']['estimated']['cost'] is None
        assert usage['data']['unknown'] and usage['data']['actual']['coverage'] == 'partial'
        assert usage['data']['resource_usage']['active_model_calls'] == 2
        assert usage['data']['resource_usage']['queued_tasks'] == 1
        with metrics.llm_inflight._lock:
            metrics.llm_inflight._vals.pop((('owner_id', services.owner_id),))
        status, missing = background_call(background_http, 'usage.get')
        assert status == 503 and missing['error']['code'] == 'HOST_NOT_READY', missing
    finally:
        with metrics.llm_inflight._lock:
            metrics.llm_inflight._vals.pop((('owner_id', services.owner_id),), None)
            if previous_global is None:
                metrics.llm_inflight._vals.pop((), None)
            else:
                metrics.llm_inflight._vals[()] = previous_global


# LLM: 原processing记录和permission事件通过规范writer发表，读取不产生决定文件。
# 函数用途: 验证真实Gateway审批列表/详情与原pending消失后的行为。
def test_native_pending_approvals_round_trip(background_http):
    from agent_py_agent.agent.gateway_parts.paths import claimed_request_chunk_path
    from agent_py_agent.agent.gateway_parts.stream_writer import write_chunk_event
    from agent_py_agent.tests.test_admin_identity_gateway import _permission, _processing_request

    services, task, runtime, run, operations, digest = seeded(background_http)
    server, base = background_http
    store = services.host.conversation_store
    threads, errors = store.threads.list_report(limit=0)
    assert not errors
    source = next(thread for thread in threads if thread.channel_bindings)
    binding = source.channel_bindings[0]
    request = _permission('reader-pending')
    record = _processing_request(server.paths, 'reader-pending', user=binding.channel_user_id,
        conversation=binding.channel_conversation_id)
    chunk = claimed_request_chunk_path(record, 'reader-pending')
    write_chunk_event(chunk, {'kind': 'permission_requested', 'permission': request.to_dict()})
    readers = wire(services, runtime)
    readers.update(native_approval_readers(base, server.paths, services.owner_id, store))
    source_page = readers['approvals']({})
    assert [row.value['approval_id'] for row in source_page.records] == [request.permission_id]
    assert source_page.records[0].value['allowed_decisions'] == []
    status, listed = background_call(background_http, 'approvals.list')
    assert status == 200, listed
    assert [item['approval_id'] for item in listed['data']['items']] == [request.permission_id]
    assert listed['data']['items'][0]['allowed_decisions'] == []
    status, detail = background_call(background_http, 'approvals.get', {'approval_id': request.permission_id})
    assert status == 200 and detail['data']['allowed_actions'] == [], detail
    assert detail['data']['approval']['revision'] == listed['data']['items'][0]['revision']
    write_chunk_event(chunk, {'kind': 'permission_resolved', 'permission_id': request.permission_id, 'decision': 'denied'})
    status, empty = background_call(background_http, 'approvals.list')
    assert status == 200 and empty['data']['items'] == [], empty


# LLM: 显式projection必须原对象保留；默认组合根须真实调用同一个四参数工厂。
# 函数用途: 验证生产root默认装配而非只测手动注入。
def test_production_composition_and_explicit_projection(background_http):
    services, task, runtime, run, operations, digest = seeded(background_http)
    fresh = api.BusinessServices(services.host)
    assert {'tool_calls', 'tool_calls.get', 'artifacts', 'artifacts.get'} <= set(fresh.observation.readers)
    explicit = BusinessObservationService(services.owner_home, owner_id=services.owner_id)
    preserved = api.BusinessServices(services.host, projection=explicit)
    assert preserved.projection is explicit and explicit.readers == {}


# LLM: 媒体登记使用原registry writer，内容读取仍沿已存在的安全HTTP运输口。
# 函数用途: 验证业务媒体登记的版本、可读引用和真实内容一致。
def test_business_media_registry_version_and_content(background_http):
    from agent_py_agent.agent.artifacts.registry import (
        ArtifactRegistration,
        register_observed_artifact,
    )
    from agent_py_agent.tests.test_mega_business_api_actions import native_request

    services, task = observed_task(background_http)
    runtime = services.observation.runtime
    initial = default_observation_readers(services.owner_home, services.owner_id, services.host.conversation_store, runtime)
    assert 'artifacts' not in initial and 'artifacts.get' not in initial
    root = services.owner_home / 'artifacts' / 'business'
    root.mkdir(parents=True, exist_ok=True)
    content = b'canonical media bytes'
    path = root / 'media-record.blob'
    path.write_bytes(content)
    sha = hashlib.sha256(content).hexdigest()
    register_observed_artifact(ArtifactRegistration(root, path, artifact_id='media-record',
        mime_type='text/plain', status='ready', source='business_upload', metadata={
            'owner_id': services.owner_id, 'space_id': task.metadata['business_context']['space_id'],
            'version_ref': 'version-' + sha[:40], 'display_name': 'evidence.txt'}),
        observed_sha256=sha, observed_size_bytes=len(content), atomic_publish=True)
    created = default_observation_readers(services.owner_home, services.owner_id, services.host.conversation_store, runtime)
    assert 'artifacts' in created and 'artifacts.get' in created
    # 不手动wire，真实root必须在首次规范媒体登记后刷新已缓存service的就绪能力。
    status, detail = background_call(background_http, 'artifacts.get', {'artifact_id': 'media-record'})
    assert status == 200 and detail['data']['available'] is True, detail
    assert detail['data']['artifact']['content_sha256'] == sha
    version = detail['data']['version_ref']
    status, headers, raw = native_request(background_http, 'GET',
        '/client/business/artifacts/media-record/content?version_ref=' + version, user='business-alice')
    assert status == 200 and raw == content
    bob, bob_task = observed_task(background_http, user='business-bob')
    seed_observed_runtime(bob, bob_task)
    status, denied = background_call(background_http, 'artifacts.get', {'artifact_id': 'media-record'}, user='business-bob')
    assert status == 404 and denied['data'] == {}, denied
    status, old = background_call(background_http, 'artifacts.get', {'artifact_id': 'media-record', 'version_ref': 'stale-version'})
    assert status == 404 and old['data'] == {}, old


# LLM: 原用量事件携task_id，当前空间相同的同线程其它task和另空间必须剔除，不靠标题或事件名。
# 函数用途: 真实HTTP验证until、space/agent/task窗口及多线程聚合。
def test_usage_window_task_and_multithread_scope(background_http):
    from agent_py_agent.agent.conversation.store_threads import create_thread_atomic
    from agent_py_agent.agent.gateway_parts.business_api_v13_readers import usage_record_visible
    from agent_py_agent.tests.test_decision_audit_controls import _decision_calls

    services, task, runtime, run, operations, digest = seeded(background_http)
    store = services.host.conversation_store
    original = store.threads.require(task.target_thread_id)
    space = task.metadata['business_context']['space_id']
    identity_key = next(key for key in original.metadata if isinstance(original.metadata[key], dict)
        and original.metadata[key].get('agent_id') == original.thread_id)
    thread_b = replace(original, thread_id='usage-agent-b', metadata={identity_key:
        dict(original.metadata[identity_key], agent_id='usage-agent-b')})
    create_thread_atomic(store.threads, thread_b)
    context_b = BusinessContextIntent(services.owner_id, space, None, None)
    task_b = store.session_tasks.create(SessionTaskDraft(task.sender_thread_id, thread_b.thread_id,
        '规范用量B', dedupe_key='scope-b'), business_context_intent=context_b,
        business_context_factory=lambda tid: BusinessExecutionContext(services.owner_id, space,
            None, tid, 1, None, (), (), 'business-context.v1'))
    other_task = store.session_tasks.create(SessionTaskDraft(task.sender_thread_id, original.thread_id,
        '同线程另一任务', dedupe_key='scope-other'), business_context_intent=context_b,
        business_context_factory=lambda tid: BusinessExecutionContext(services.owner_id, space,
            None, tid, 1, None, (), (), 'business-context.v1'))
    now = time.time()
    for event_id, tid, task_id, tokens, stamp in (
            ('scope-a', original.thread_id, task.task_id, 123, now - 20),
            ('scope-b', thread_b.thread_id, task_b.task_id, 77, now - 10),
            ('same-thread-other-task', original.thread_id, other_task.task_id, 41, now - 15),
            ('outside-before', original.thread_id, task.task_id, 5000, now - 100000),
            ('outside-after', original.thread_id, task.task_id, 7000, now + 20)):
        store.model_usage.append_snapshot_once({'thread_id': tid, 'request_id': event_id, 'run_id': event_id,
            'task_id': task_id, 'source': 'test', 'model_calls': _decision_calls(finished=1, timed_out=0,
                input_tokens=tokens), 'now': stamp})
    metrics = concurrency_metrics._metrics()
    metrics.llm_inflight.set(0, labels={'owner_id': services.owner_id})
    try:
        readers = wire(services, runtime)
        filters = {'since': datetime.fromtimestamp(now - 86400, timezone.utc).isoformat(),
            'until': datetime.fromtimestamp(now, timezone.utc).isoformat(), 'space_id': space}
        source_page = readers['usage']({'filter': filters})
        assert source_page.records[0].value['actual']['input_tokens'] == 241
        full_actor = BusinessActor(services.owner_id, 'all', services.observation, lambda action, facts: True)
        assert usage_record_visible(services.observation, source_page.records[0], {'filter': filters}, full_actor)
        status, aggregate = background_call(background_http, 'usage.get', {'filter': filters})
        assert status == 200 and aggregate['data']['actual']['input_tokens'] == 241, aggregate
        status, one = background_call(background_http, 'usage.get', {'filter': dict(filters, agent_id=original.thread_id)})
        assert status == 200 and one['data']['actual']['input_tokens'] == 164, one
        status, exact = background_call(background_http, 'usage.get', {'filter': dict(filters, task_id=task.task_id)})
        assert status == 200 and exact['data']['actual']['input_tokens'] == 123, exact
        page = readers['usage']({'filter': filters})
        # 聚合任一成员撤权则整行不可见，不泄露剩余成员或用owner授权顶替线程授权。
        actor = BusinessActor(services.owner_id, 'scoped', services.observation,
            lambda action, facts: facts.get('thread_id') != thread_b.thread_id)
        assert not usage_record_visible(services.observation, page.records[0], {'filter': filters}, actor)
    finally:
        with metrics.llm_inflight._lock:
            metrics.llm_inflight._vals.pop((('owner_id', services.owner_id),), None)


# LLM: 参数和结算均走规范写口；嵌套结果使用原 coordinator 序列化，不写展示输入。
# 函数用途: 创建真实 managed 操作，可停在未结算阶段供边界测试复用。
def managed_roundtrip_operation(services, task, *, arguments=None, tool='scripts', raw=None,
                                settled=True, ok=True):
    import os
    from uuid import uuid4

    from agent_py_agent.agent.local_storage.tool_operations import (
        ToolOperationClaimRequest,
        ToolOperationCompletionRequest,
        ToolOperationHolder,
    )
    from agent_py_agent.agent.runtime_db.managed_operation_store import ManagedOperationStore
    from agent_py_agent.agent.tooling.runtime_contracts import tool_arguments_hash
    from agent_py_agent.agent.tooling.tool_operation_coordinator import (
        ToolHandlerOutcome,
        _result_payload,
    )

    runtime, run = seed_observed_runtime(services, task)
    operation_id = 'roundtrip-' + uuid4().hex
    operations = ManagedOperationStore(runtime)
    args = arguments if arguments is not None else {'action': 'run', 'script_id': 'collect', 'inputs': {}}
    claimed = operations.claim_tool_operation(ToolOperationClaimRequest(
        owner_id=services.owner_id, run_id='observed-' + task.task_id, task_id=run['task_id'],
        operation_id=operation_id, tool=tool, args_hash=tool_arguments_hash(args),
        idempotency_key=operation_id, idempotency_scope='operation', idempotency_namespace='roundtrip',
        holder=ToolOperationHolder('roundtrip-holder', 'localhost', os.getpid(), 'roundtrip-process'),
        lease_expires_at=time.time() + 900, attempt_id=run['attempt_id']))
    assert claimed.action == 'execute'
    result_ref = 'tool-operation:' + operation_id
    payload = _result_payload(ToolHandlerOutcome(tool, ok, json.dumps(raw or {'value': 7}),
        call_id=operation_id, result_envelope={'tool_operation': {'result_ref': result_ref}},
        handler_executed=True, effect_outcome='' if ok else 'failed', duration_ms=17))
    if settled:
        operations.finish_tool_operation(ToolOperationCompletionRequest(
            services.owner_id, 'observed-' + task.task_id, operation_id,
            claimed.record.holder_id, claimed.record.generation, 'succeeded' if ok else 'failed', payload))
    with runtime._runtime_connection() as conn:
        outcome = json.loads(conn.execute('SELECT outcome_json FROM tool_operations WHERE operation_id=?',
            (operation_id,)).fetchone()[0])
    assert 'redacted_input' not in outcome and outcome['args_hash'] == tool_arguments_hash(args)
    return runtime, run, operation_id, payload


# LLM: 真实 writer→reader→service→HTTP→冻结 schema 校验，没有手写服务端响应。
# 函数用途: 证明嵌套结算事实被精确投影，未结算引用不代表完成。
@pytest.mark.parametrize('settled', [True, False])
def test_150_managed_writer_observation_http_roundtrip(background_http, settled):
    from agent_py_agent.agent.plugin_host.business_action_specs import business_action_spec

    services, task = observed_task(background_http)
    runtime, run, operation_id, payload = managed_roundtrip_operation(services, task, settled=settled)
    wire(services, runtime)
    actor = BusinessActor(services.owner_id, 'roundtrip', services.observation, lambda action, facts: True)
    projected = services.observation.query('tool_calls.get', {'operation_id': operation_id}, actor=actor)
    status, body = background_call(background_http, 'tool_calls.get', {'operation_id': operation_id})
    assert status == 200, body
    data = business_action_spec('tool_calls.get').validate_result(body['data'])
    assert data == projected
    assert data['redacted_input'] == {}
    assert data['tool_call']['task_id'] == task.task_id
    assert data['tool_call']['run_id'] == 'observed-' + task.task_id
    assert data['tool_call']['attempt_id'] == run['attempt_id']
    assert data['tool_call']['executed'] is True
    assert data['effect_outcome'] == ('unknown' if settled else 'running')
    assert data['result_ref'] == (payload['result_ref'] if settled else operation_id)
    assert data['tool_call']['duration_ms'] == (17 if settled else None)
    assert data['tool_call']['status'] == ('SUCCEEDED' if settled else 'EXECUTING')


# LLM: 已结算的坏协议不能降级成似乎正常的运行中数据，保留来源 unknown。
# 函数用途: 检查嵌套 schema、tool 与事实类型的拒绝边界。
@pytest.mark.parametrize('change', [
    {'schema_version': 'other.v1'}, {'tool': 'other'}, {'result_ref': ''},
    {'effect_outcome': 'confirmed'}, {'handler_executed': 'true'}, {'duration_ms': -1},
])
def test_150_invalid_nested_result_is_unknown(background_http, change):
    services, task = observed_task(background_http)
    runtime, run, operation_id, payload = managed_roundtrip_operation(services, task)
    with runtime._runtime_connection() as conn:
        outcome = json.loads(conn.execute('SELECT outcome_json FROM tool_operations WHERE operation_id=?',
            (operation_id,)).fetchone()[0])
        outcome['result'].update(change)
        conn.execute('UPDATE tool_operations SET outcome_json=? WHERE operation_id=?',
            (json.dumps(outcome), operation_id))
        conn.commit()
    wire(services, runtime)
    status, body = background_call(background_http, 'tool_calls.list')
    assert status == 200 and body['data']['coverage'] == 'unknown' and body['data']['items'] == [], body
    status, body = background_call(background_http, 'tool_calls.get', {'operation_id': operation_id})
    assert status == 503 and body['error']['code'] == 'HOST_NOT_READY', body


# LLM: 静态规则只约束这次协议边界，正例允许嵌套别名读取，反例覆盖旧外层读取和展示字段证明。
# 函数用途: 收集 #150 的错误读取模式，用同一规则检查生产与最小反例。
def _150_wire_contract_gaps(source):
    import ast

    gaps = []
    for function in ast.walk(ast.parse(source)):
        if not isinstance(function, ast.FunctionDef):
            continue
        for node in ast.walk(function):
            if (function.name == 'tool_calls' and isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute) and node.func.attr == 'get'
                    and isinstance(node.func.value, ast.Name) and node.func.value.id == 'outcome'
                    and node.args and isinstance(node.args[0], ast.Constant)
                    and node.args[0].value in ('result_ref', 'effect_outcome', 'redacted_input')):
                gaps.append(node.args[0].value)
            if (function.name == '_task_script_result' and isinstance(node, ast.Constant)
                    and node.value == 'redacted_input'):
                gaps.append('redacted_input')
    return gaps


def test_150_ast_contract_guard_positive_and_negative():
    from pathlib import Path

    from agent_py_agent.agent.business_spaces import v13_w7_scripts
    from agent_py_agent.agent.gateway_parts import business_api_v13_readers

    for module in (business_api_v13_readers, v13_w7_scripts):
        assert _150_wire_contract_gaps(Path(module.__file__).read_text()) == []
    assert _150_wire_contract_gaps('def tool_calls():\n return outcome.get("effect_outcome"), outcome.get("result_ref")') == ['effect_outcome', 'result_ref']
    assert _150_wire_contract_gaps('def _task_script_result():\n return details["redacted_input"]') == ['redacted_input']
    assert _150_wire_contract_gaps('def tool_calls():\n result = outcome["result"]\n return result["result_ref"], result["effect_outcome"]') == []
