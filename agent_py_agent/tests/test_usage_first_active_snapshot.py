"""首个模型尚未持久计量时，真实HTTP观察保留已知资源和规范范围。"""
import pytest

from agent_py_agent.tests import test_acceptance_observation_projections as acceptance
from agent_py_agent.tests import test_v13_w0_observation_readers as original
from agent_py_agent.tests.test_mega_business_api_actions import background_call

background_http = original.background_http
isolated_home = original.isolated_home
sampled_owner = acceptance.sampled_owner


@pytest.mark.parametrize('inflight', [0, 2])
@pytest.mark.parametrize('scope', ['run_id', 'task_id', 'agent_id', 'space_id'])
def test_first_active_snapshot_without_usage_is_known_resource_not_zero_tokens(background_http, sampled_owner, scope, inflight):
    services, task, runtime, run = sampled_owner
    from agent_py_agent.agent.observability import concurrency_metrics
    concurrency_metrics._metrics().llm_inflight.set(inflight, labels={'owner_id': services.owner_id})
    context = task.metadata['business_context']
    values = {'run_id': 'observed-' + task.task_id, 'task_id': task.task_id,
        'agent_id': task.target_thread_id, 'space_id': context['space_id']}
    original.wire(services, runtime)
    for _ in range(2):
        status, result = background_call(background_http, 'usage.get', {'filter': {scope: values[scope]}})
        assert status == 200, result
        data = result['data']
        assert data['actual']['input_tokens'] is None
        assert data['estimated']['input_tokens'] is None
        assert data['actual']['coverage'] == 'unknown'
        assert data['unknown'] == ['usage_source_unavailable']
        assert data['resource_usage']['queued_tasks'] == 1
        assert data['resource_usage']['active_model_calls'] == inflight
    assert services.host.conversation_store.session_tasks.require(task.task_id).status == 'queued'


def test_first_active_scope_rejects_unwitnessed_and_conflicting_filters(background_http, sampled_owner):
    services, task, runtime, run = sampled_owner
    original.wire(services, runtime)
    for filters in ({'run_id': 'nonexistent'},
                    {'run_id': 'observed-' + task.task_id, 'agent_id': 'foreign-thread'},
                    {'run_id': 'observed-' + task.task_id, 'space_id': 'foreign-space'},
                    {'attempt_id': run['attempt_id']}):
        status, result = background_call(background_http, 'usage.get', {'filter': filters})
        assert status != 200, result
        assert result['data'] == {}


def test_foreign_canonical_run_cannot_witness_empty_usage(background_http, sampled_owner):
    services, task, runtime, run = sampled_owner
    runtime.record_run_creation(owner_id='other-owner', run_id='foreign-owned-run',
        thread_id=task.target_thread_id)
    original.wire(services, runtime)
    status, result = background_call(background_http, 'usage.get', {'filter': {'run_id': 'foreign-owned-run'}})
    assert status != 200 and result['data'] == {}, result


def test_empty_run_with_existing_other_usage_keeps_canonical_scope(background_http, sampled_owner):
    services, task, runtime, run = sampled_owner
    usage = services.host.conversation_store.model_usage
    usage.append_snapshot_once({'thread_id': task.target_thread_id, 'request_id': 'other-usage',
        'run_id': 'another-run', 'task_id': task.task_id, 'source': 'test',
        'model_calls': acceptance._decision_calls(finished=1, timed_out=0, input_tokens=9), 'now': 1})
    original.wire(services, runtime)
    status, result = background_call(background_http, 'usage.get', {'filter': {'run_id': 'observed-' + task.task_id}})
    assert status == 200, result
    assert result['data']['actual']['input_tokens'] is None
    assert result['data']['resource_usage']['queued_tasks'] == 1


def test_first_usage_reversed_time_window_still_refused(background_http, sampled_owner):
    services, task, runtime, run = sampled_owner
    original.wire(services, runtime)
    status, result = background_call(background_http, 'usage.get', {'filter': {
        'task_id': task.task_id, 'since': '2026-11-14T00:00:00Z', 'until': '2026-11-13T00:00:00Z'}})
    assert status != 200 and result['data'] == {}, result


def test_first_usage_runtime_read_error_does_not_manufacture_scope(background_http, sampled_owner, monkeypatch):
    services, task, runtime, run = sampled_owner
    original.wire(services, runtime)
    source = services.observation.readers['resource_usage'].__self__

    def unavailable():
        raise OSError('synthetic canonical source unavailable')

    monkeypatch.setattr(source.repo, '_runtime_connection', unavailable)
    from agent_py_agent.agent.conversation.business_access import BusinessActor, BusinessError
    actor = BusinessActor(services.owner_id, 'test', services.observation, lambda action, facts: True)
    # 原HTTP Handler会每请求重装reader；精确故障只注入此已装配只读源，不替换响应。
    with pytest.raises(BusinessError) as denied:
        services.observation.query('usage.get', {'filter': {'run_id': 'observed-' + task.task_id}}, actor=actor)
    assert denied.value.code == 'HOST_NOT_READY'
