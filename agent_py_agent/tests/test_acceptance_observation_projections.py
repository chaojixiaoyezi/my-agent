"""验收反例复核：原producer→默认reader→生产BusinessClient→真实HTTP。

不接真实provider；只替换工具主体/模型响应，操作、用量和HTTP协议走原实现。
"""
from __future__ import annotations

import json
import time
from dataclasses import replace
from uuid import uuid4

import pytest

from agent_py_agent.agent.common.log_redaction import redact_sensitive_value
from agent_py_agent.agent.conversation.business_access import (
    BusinessActor,
    BusinessError,
)
from agent_py_agent.agent.runtime_db.managed_operation_store import (
    ManagedOperationStore,
)
from agent_py_agent.agent.tooling.models import ToolHandlerOutcome
from agent_py_agent.agent.tooling.runtime_contracts import tool_arguments_hash
from agent_py_agent.agent.tooling.tool_operation_coordinator import (
    ToolOperationExecutionRequest,
    execute_tool_operation,
)
from agent_py_agent.cli.space_commands import BusinessClient
from agent_py_agent.tests import test_v13_w0_observation_readers as original
from agent_py_agent.tests.test_decision_audit_controls import _decision_calls
from agent_py_agent.tests.test_mega_business_api_actions import (
    native_request,
    seed_observed_runtime,
)

background_http = original.background_http
isolated_home = original.isolated_home


def client_for(background, *, user='business-alice'):
    # 只装配真实socket运输；请求冻结、封套验证和结果解析全部属于生产client。
    def transport(path, payload, *, timeout):
        status, _, raw = native_request(background, 'POST', path, payload, user=user)
        return status, json.loads(raw)
    return BusinessClient(transport)


def produced_operation(services, task, *, effect='', settled_effect='committed', ok=True,
                       duration=38, error_code='', handler_executed=True):
    runtime, run = seed_observed_runtime(services, task)
    operations = ManagedOperationStore(runtime)
    operation_id = 'acceptance-' + uuid4().hex
    args = {'action': 'pause', 'agent_id': task.target_thread_id,
            'inputs': {'api_key': 'synthetic-acceptance-secret', 'limit': 7}}
    invokes = []

    def fake_tool():
        invokes.append(operation_id)
        return ToolHandlerOutcome('background_agents', ok, '{"paused":true}',
            call_id=operation_id, error_code=error_code, effect_outcome=effect,
            handler_executed=handler_executed, duration_ms=duration)

    request = ToolOperationExecutionRequest(store=operations, store_required=True,
        owner_id=services.owner_id, run_id='observed-' + task.task_id, task_id=run['task_id'],
        operation_id=operation_id, tool_name='background_agents', args_hash=tool_arguments_hash(args),
        idempotency_key=operation_id, idempotency_scope='operation', idempotency_namespace='acceptance',
        timeout_seconds=10, invoke=fake_tool, attempt_id=run['attempt_id'],
        redacted_input=redact_sensitive_value(args), settled_effect_outcome=settled_effect)
    result = execute_tool_operation(request)
    assert invokes == [operation_id]
    return runtime, run, operation_id, result, request


def stored_outcome(runtime, operation_id):
    with runtime._runtime_connection() as conn:
        row = conn.execute('SELECT status,outcome_json FROM tool_operations WHERE operation_id=?',
                           (operation_id,)).fetchone()
    return row['status'], json.loads(row['outcome_json'])


@pytest.mark.parametrize('effect,settled_effect,ok,duration,error_code,expected_effect', [
    ('', 'committed', True, 38, '', 'committed'),
    ('', 'none', True, 0, '', 'none'),
    ('failed', '', False, 38, 'PERMISSION_DENIED', 'unknown'),
    ('unknown', '', False, 38, 'TIMEOUT', 'unknown'),
])
def test_producer_tool_facts_survive_standard_client(background_http, effect, settled_effect,
                                                    ok, duration, error_code, expected_effect):
    services, task = original.observed_task(background_http)
    runtime, run, operation_id, result, request = produced_operation(services, task,
        effect=effect, settled_effect=settled_effect, ok=ok, duration=duration, error_code=error_code)
    before = stored_outcome(runtime, operation_id)
    payload = before[1]['result']
    assert payload['duration_ms'] == duration and payload['handler_executed'] is True
    assert payload['redacted_input']['action'] == 'pause'
    assert payload['effect_outcome'] == expected_effect or (effect == 'failed' and payload['effect_outcome'] == 'failed')
    assert 'synthetic-acceptance-secret' not in json.dumps(payload['redacted_input'])

    original.wire(services, runtime)
    actor = BusinessActor(services.owner_id, 'acceptance', services.observation, lambda action, facts: True)
    direct = services.observation.query('tool_calls.get', {'operation_id': operation_id}, actor=actor)
    client = client_for(background_http)
    detail = client.call('tool_calls.get', {'operation_id': operation_id})
    assert detail['ok'] and detail['http_status'] == 200, detail
    assert detail['data'] == direct
    value = detail['data']
    assert value['tool_call']['duration_ms'] == duration
    assert value['tool_call']['executed'] is True
    assert value['tool_call']['attempt_id'] == run['attempt_id']
    assert value['tool_call']['task_id'] == task.task_id
    assert value['tool_call']['status'] == before[0]
    assert value['effect_outcome'] == expected_effect
    assert value['result_ref'] == payload['result_ref']
    assert value['redacted_input'] == payload['redacted_input']
    assert 'synthetic-acceptance-secret' not in json.dumps(detail)
    if not ok:
        assert value['tool_call']['error']['code'] == before[1]['error_code']
    listed = client.call('tool_calls.list', {'filter': {'task_id': task.task_id}})
    assert listed['ok'], listed
    assert listed['data']['items'] == [value['tool_call']]
    assert stored_outcome(runtime, operation_id) == before  # 观察不可回写/补封原操作。
    if ok:
        def forbidden_reexecution():
            pytest.fail('成功规范操作重放不应再次调用工具')
        replay = execute_tool_operation(replace(request, invoke=forbidden_reexecution))
        assert replay.ok
        assert stored_outcome(runtime, operation_id) == before


@pytest.mark.parametrize('unknown', [False, True])
def test_tool_client_scope_and_revocation(background_http, unknown):
    services, task = original.observed_task(background_http)
    runtime, _, operation_id, _, _ = produced_operation(services, task,
        effect='unknown' if unknown else '', ok=not unknown)
    original.wire(services, runtime)
    client = client_for(background_http)
    other_space = client.call('tool_calls.list', {'filter': {'space_id': 'not-the-space'}})
    assert other_space['ok'] and other_space['data']['items'] == []
    bob, bob_task = original.observed_task(background_http, user='business-bob')
    bob_runtime, _ = seed_observed_runtime(bob, bob_task)
    original.wire(bob, bob_runtime)
    foreign = client_for(background_http, user='business-bob').call('tool_calls.get', {'operation_id': operation_id})
    assert not foreign['ok'] and foreign['http_status'] == 404, foreign
    denied = BusinessActor(services.owner_id, 'revoked', services.observation, lambda action, facts: False)
    with pytest.raises(BusinessError, match='ACCESS_DENIED'):
        services.observation.query('tool_calls.get', {'operation_id': operation_id}, actor=denied)


@pytest.fixture
def sampled_owner(background_http):
    from agent_py_agent.agent.observability import concurrency_metrics

    services, task = original.observed_task(background_http)
    runtime, run = seed_observed_runtime(services, task)
    metrics = concurrency_metrics._metrics()
    key = (('owner_id', services.owner_id),)
    with metrics.llm_inflight._lock:
        previous = metrics.llm_inflight._vals.get(key)
    metrics.llm_inflight.set(0, labels={'owner_id': services.owner_id})
    try:
        yield services, task, runtime, run
    finally:
        with metrics.llm_inflight._lock:
            if previous is None:
                metrics.llm_inflight._vals.pop(key, None)
            else:
                metrics.llm_inflight._vals[key] = previous


def sent_timeout_calls():
    from types import SimpleNamespace

    from agent_py_agent.agent.agent_core.model.call_runtime import (
        ModelCallTimeoutFacts,
        model_call_summary,
        record_model_call_timeout,
    )
    from agent_py_agent.agent.contracts.model_call_ledger import (
        ModelCallLedger,
        ModelCallProviderAttemptParams,
        ModelCallStartedParams,
    )

    ledger = ModelCallLedger()
    ledger.started(ModelCallStartedParams('sent-timeout', 'fake', 'fake', 42,
        request_id='req', run_id='run', metadata={'purpose': 'decision'}))
    ledger.provider_attempt(ModelCallProviderAttemptParams('sent-timeout', 'http-1', 'started'))
    record_model_call_timeout(ledger, ModelCallTimeoutFacts('sent-timeout', 2, 'wall_clock'))
    return model_call_summary(SimpleNamespace(_model_call_ledger=ledger), request_id='req', run_id='run')


@pytest.mark.parametrize('finished,timed_out,input_tokens,sent,actual,estimated', [
    (1, 0, 321, False, 321, None),
    (1, 0, 0, False, 0, None),
    (0, 1, 0, False, None, None),
    (0, 1, 0, True, None, 42),
])
def test_usage_model_receipt_zero_and_estimate_are_distinct(background_http, sampled_owner,
                                                          finished, timed_out, input_tokens, sent, actual, estimated):
    services, task, runtime, run = sampled_owner
    usage = services.host.conversation_store.model_usage
    calls = sent_timeout_calls() if sent else _decision_calls(
        finished=finished, timed_out=timed_out, input_tokens=input_tokens)
    request = {'thread_id': task.target_thread_id, 'request_id': 'acceptance-model',
        'run_id': 'observed-' + task.task_id, 'task_id': task.task_id,
        'source': 'test-fake-model-response', 'model_calls': calls, 'now': time.time()}
    usage.append_snapshot_once(request)
    usage.append_snapshot_once(request)  # 同一快照不能重复计费。
    before = usage._path(task.target_thread_id).read_bytes()
    original.wire(services, runtime)
    client = client_for(background_http)
    response = client.call('usage.get', {'filter': {'task_id': task.task_id}})
    assert response['ok'], response
    data = response['data']
    assert data['actual']['input_tokens'] == actual
    assert data['estimated']['input_tokens'] == estimated
    for bucket in ('actual', 'estimated'):
        assert data[bucket]['cost'] is None and data[bucket]['currency'] is None
    assert data['actual']['coverage'] == 'partial'
    assert 'decision_usage_only' in data['unknown'] and 'cost_unavailable' in data['unknown']
    assert data['resource_usage']['active_model_calls'] == 0
    assert data['resource_usage']['queued_tasks'] == 1  # queued并非活跃模型占用。
    assert usage._path(task.target_thread_id).read_bytes() == before


def test_missing_usage_source_does_not_invent_zero(background_http, sampled_owner):
    services, task, runtime, run = sampled_owner
    original.wire(services, runtime)
    response = client_for(background_http).call('usage.get', {})
    assert response['ok'], response
    assert response['data']['actual']['input_tokens'] is None
    assert response['data']['estimated']['input_tokens'] is None
    assert response['data']['unknown'] == ['usage_source_unavailable']
    assert response['data']['actual']['coverage'] == 'unknown'


def test_bad_usage_source_is_not_complete_or_zero(background_http, sampled_owner):
    services, task, runtime, run = sampled_owner
    usage = services.host.conversation_store.model_usage
    usage.append_snapshot_once({'thread_id': task.target_thread_id, 'request_id': 'broken-later',
        'run_id': 'observed-' + task.task_id, 'task_id': task.task_id, 'source': 'test',
        'model_calls': _decision_calls(finished=1, timed_out=0, input_tokens=9), 'now': time.time()})
    with usage._path(task.target_thread_id).open('ab') as handle:
        handle.write(b'{broken\n')
    original.wire(services, runtime)
    response = client_for(background_http).call('usage.get', {'filter': {'task_id': task.task_id}})
    assert not response['ok'] and response['http_status'] == 503, response
    assert response['error']['code'] == 'HOST_NOT_READY'


def test_unknown_zero_false_receipt_and_retry_do_not_upgrade_original(background_http):
    services, task = original.observed_task(background_http)
    runtime, _, operation_id, _, request = produced_operation(services, task,
        effect='unknown', ok=False, duration=0, handler_executed=False)
    before = stored_outcome(runtime, operation_id)
    assert before[0] == 'UNKNOWN'
    assert before[1]['result']['handler_executed'] is False
    original.wire(services, runtime)
    response = client_for(background_http).call('tool_calls.get', {'operation_id': operation_id})
    assert response['ok'], response
    view = response['data']['tool_call']
    assert view['status'] == 'UNKNOWN' and view['effect_outcome'] == 'unknown'
    assert view['duration_ms'] == 0 and view['executed'] is False
    assert view['result_ref'] == before[1]['result']['result_ref']

    def forbidden_retry():
        pytest.fail('UNKNOWN不能因为展示回执而得到再次执行权')

    retry = execute_tool_operation(replace(request, invoke=forbidden_retry))
    assert not retry.ok and retry.handler_executed is False
    assert retry.effect_outcome == 'not_started'
    assert stored_outcome(runtime, operation_id) == before


@pytest.mark.parametrize('change', [
    {'schema_version': 'wrong.v1'}, {'tool': 'wrong'}, {'duration_ms': None},
    {'duration_ms': -1}, {'duration_ms': float('nan')}, {'handler_executed': 'true'},
    {'result_ref': ''},
])
def test_corrupt_unknown_receipt_stays_unavailable(background_http, change):
    # 故障注入只损坏临时原行，正例保真仍由原producer证明；不替产品补规范事实。
    services, task = original.observed_task(background_http)
    runtime, _, operation_id, _, _ = produced_operation(services, task, effect='unknown', ok=False)
    status, outcome = stored_outcome(runtime, operation_id)
    assert status == 'UNKNOWN'
    outcome['result'].update(change)
    with runtime._runtime_connection() as conn:
        conn.execute('UPDATE tool_operations SET outcome_json=? WHERE operation_id=?',
                     (json.dumps(outcome), operation_id))
        conn.commit()
    original.wire(services, runtime)
    client = client_for(background_http)
    listed = client.call('tool_calls.list', {})
    assert listed['ok'] and listed['data']['coverage'] == 'unknown'
    assert listed['data']['items'] == []
    detail = client.call('tool_calls.get', {'operation_id': operation_id})
    assert not detail['ok'] and detail['http_status'] == 503, detail
    assert detail['error']['code'] == 'HOST_NOT_READY'


@pytest.mark.parametrize('state', ['UNKNOWN', 'EXECUTING'])
def test_absent_or_live_receipt_is_not_a_settlement(background_http, state):
    services, task = original.observed_task(background_http)
    if state == 'UNKNOWN':
        runtime, _, operation_id, _, _ = produced_operation(services, task, effect='unknown', ok=False)
        _, outcome = stored_outcome(runtime, operation_id)
        outcome.pop('result')
        # 旧UNKNOWN确实可能没有回执；缺失仍未知，不凭handler时间伪造0ms。
    else:
        runtime, _, operation_id, payload = original.managed_roundtrip_operation(services, task, settled=False)
        _, outcome = stored_outcome(runtime, operation_id)
        outcome['result'] = payload
        # 活跃行即使带结构完整的旧回执也不算结算，不能拿它签发本次执行结果。
    with runtime._runtime_connection() as conn:
        conn.execute('UPDATE tool_operations SET outcome_json=? WHERE operation_id=?',
                     (json.dumps(outcome), operation_id))
        conn.commit()
    original.wire(services, runtime)
    response = client_for(background_http).call('tool_calls.get', {'operation_id': operation_id})
    assert response['ok'], response
    view = response['data']['tool_call']
    assert view['status'] == state
    assert view['effect_outcome'] == ('unknown' if state == 'UNKNOWN' else 'running')
    assert view['duration_ms'] is None and view['result_ref'] == operation_id
