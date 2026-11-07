"""W3 重试、租户桶与模型选择合同；后端和时钟均为确定性替身。"""
from dataclasses import fields
from io import BytesIO
from types import SimpleNamespace
from urllib.error import HTTPError, URLError

import pytest

from agent_py_agent.agent.backends import gateway_helpers as gateway
from agent_py_agent.agent.backends.http import HttpBackend
from agent_py_agent.agent.llm_scale.rate_limiter import TenantRateLimiter


def request(**values):
    return gateway.GatewayRequest(api_base='https://provider.invalid', api_key='test',
        path='/model', payload={}, headers={}, timeout=30, **values)


def test_retry_envelope_default_is_anchor_tuple(monkeypatch):
    assert 'retry_backoff_seconds' in {item.name for item in fields(gateway.GatewayRequest)}
    assert request().retry_backoff_seconds == ()
    waits, attempts = [], []

    def open_request(*args, **kwargs):
        attempts.append(1)
        raise HTTPError('https://provider.invalid', 503, 'temporary', {}, BytesIO(b'{}'))

    monkeypatch.setattr(gateway, '_gateway_urlopen', open_request)
    monkeypatch.setattr(gateway, '_provider_retry_wait', waits.append)
    with pytest.raises(HTTPError):
        gateway._open_gateway_request(request())
    assert waits == [2.0, 5.0, 15.0]
    assert len(attempts) == 4


@pytest.mark.parametrize('network', [False, True])
def test_request_retry_override_controls_count_and_delays(monkeypatch, network):
    assert 'retry_backoff_seconds' in {item.name for item in fields(gateway.GatewayRequest)}
    waits, attempts = [], []

    def open_request(*args, **kwargs):
        attempts.append(1)
        if network:
            raise URLError('connection reset')
        raise HTTPError('https://provider.invalid', 503, 'temporary', {}, BytesIO(b'{}'))

    monkeypatch.setattr(gateway, '_gateway_urlopen', open_request)
    monkeypatch.setattr(gateway, '_provider_retry_wait', waits.append)
    with pytest.raises((HTTPError, URLError)):
        gateway._open_gateway_request(request(retry_backoff_seconds=(0.0, 7.0)))
    assert len(attempts) == 3
    assert waits == [0.0, 7.0]
    attempts.clear()
    waits.clear()
    with pytest.raises((HTTPError, URLError)):
        gateway._open_gateway_request(request(retry_backoff_seconds=(0.0, 7.0), max_retries=0))
    assert len(attempts) == 1 and waits == []


def test_retry_after_keeps_original_cap(monkeypatch):
    assert 'retry_backoff_seconds' in {item.name for item in fields(gateway.GatewayRequest)}
    waits = []

    def open_request(*args, **kwargs):
        raise HTTPError('https://provider.invalid', 429, 'rate', {'Retry-After': '900'}, BytesIO(b'{}'))

    monkeypatch.setattr(gateway, '_gateway_urlopen', open_request)
    monkeypatch.setattr(gateway, '_provider_retry_wait', waits.append)
    with pytest.raises(HTTPError):
        gateway._open_gateway_request(request(retry_backoff_seconds=(1.0,)))
    assert waits == [30.0]


def test_backend_freezes_config_retry_override_in_request():
    backend = SimpleNamespace(api_base='https://provider.invalid', api_key='test', request_timeout=30,
        connect_timeout=10, custom_headers={}, session_header='', model_http_max_idle_connections=0,
        config=SimpleNamespace(model_retry_backoff_seconds=[0.0, 3.0]))
    built = HttpBackend._gateway_request(backend, '/model', {}, {})
    assert getattr(built, 'retry_backoff_seconds', None) == (0.0, 3.0)
    backend.config.model_retry_backoff_seconds.append(9.0)
    assert built.retry_backoff_seconds == (0.0, 3.0)
    backend.config = SimpleNamespace()
    assert HttpBackend._gateway_request(backend, '/model', {}, {}).retry_backoff_seconds == ()


def test_tenant_reset_is_scoped_and_does_not_iterate_other_buckets():
    limiter = TenantRateLimiter(0, 2, clock=lambda: 0)
    assert callable(getattr(limiter, 'reset', None))
    assert limiter.allow('alice', 2) and limiter.allow('bob', 2)
    assert not limiter.allow('alice') and not limiter.allow('bob')

    class PointOnly(dict):
        def __iter__(self):
            raise AssertionError('reset must not scan tenants')

        def items(self):
            raise AssertionError('reset must not scan tenants')

    limiter._buckets = PointOnly(limiter._buckets)
    limiter.reset('alice')
    assert limiter.allow('alice', 2)
    assert not limiter.allow('bob')
    limiter.reset('missing')
    assert limiter.tenant_count() == 2


# 复用同一原生 owner 夹具，两个新测试文件不复制生产响应。
from agent_py_agent.tests.test_gap_w3_side_questions import w3_api as w3_api  # noqa: E402


def test_pool_status_projects_original_admission_and_owner_ledger(w3_api, monkeypatch):
    from agent_py_agent.agent.agent_core.model.call_runtime import model_call_ledger
    from agent_py_agent.agent.contracts.model_call_ledger import ModelCallStartedParams
    from agent_py_agent.agent.llm_scale import hot_path
    from agent_py_agent.agent.llm_scale.concurrency import ConcurrencyLimiter

    api = w3_api
    limiter = ConcurrencyLimiter(4)
    monkeypatch.setattr(hot_path, '_RESOLVED', True)
    monkeypatch.setattr(hot_path, '_LIMITER', limiter)
    ledger = model_call_ledger(api.agent)
    ledger.started(ModelCallStartedParams(call_id='pool-call', backend='fake', model='fake-model', input_tokens=3))
    with limiter.slot(timeout=0):
        result = api.invoke('llm_pool.status')
    assert result['admission']['global_in_flight'] == 1
    assert result['admission']['global_max_in_flight'] == 4
    model = next(row for row in result['models'] if row['model'] == 'fake-model')
    assert model['in_flight'] == 1 and model['recent_calls'] == 1 and model['recent_failures'] == 0
    assert result['retry_policy']['backoff_seconds'] == [2.0, 5.0, 15.0]


def test_pool_reset_receipt_does_not_reset_global_limiter(w3_api):
    api = w3_api
    rate = TenantRateLimiter(0, 1, clock=lambda: 0)
    api.agent.tenant_rate_limiter = rate
    assert rate.allow(api.services.owner_id)
    assert not rate.allow(api.services.owner_id)
    receipt = api.invoke('llm_pool.reset', {'scope': 'owner_rate_bucket'})
    assert receipt == {'reset': ['owner_rate_bucket'], 'unchanged': []}
    assert rate.allow(api.services.owner_id)


@pytest.mark.parametrize('value', [[], [-1], [301], [True], [float('inf')], [1] * 9])
def test_retry_policy_rejects_invalid_sequences(w3_api, value):
    from agent_py_agent.agent.plugin_host.errors import HostAPIError
    with pytest.raises(HostAPIError) as error:
        w3_api.invoke('llm_pool.retry_policy.set', {'backoff_seconds': value})
    assert error.value.code == 'INVALID_SCHEMA'


def test_retry_writer_dependency_is_not_replaced_by_direct_yaml(w3_api, monkeypatch, tmp_path):
    from agent_py_agent.agent.business_spaces import gap158_w3_llm_pool as pool
    from agent_py_agent.agent.plugin_host.errors import HostAPIError
    from agent_py_agent.agent.settings.parameter_registry import parameter_registry
    from agent_py_agent.agent.settings.user_config_capability import user_config_path

    api = w3_api
    spec = parameter_registry().get('model_retry_backoff_seconds')
    if spec is None or not spec.writer:
        before = list(api.services.owner_home.iterdir())
        with pytest.raises(HostAPIError) as error:
            api.invoke('llm_pool.retry_policy.set', {'backoff_seconds': [0, 2]})
        assert error.value.code == 'HOST_NOT_READY'
        assert list(api.services.owner_home.iterdir()) == before
    # 契约层另测原写服务路由；不把这个替身结果称为 W0 的真实写入验收。
    called = []
    monkeypatch.setattr(pool, '_settings_path', lambda call: tmp_path / 'settings.yaml')

    def writer(key, value, *, paths, origin):
        called.append((key, value, paths.user_path))
        return {'ok': True}

    monkeypatch.setattr(pool, 'set_parameter', writer)
    policy = api.invoke('llm_pool.retry_policy.set', {'backoff_seconds': [0, 2]})['policy']
    assert called == [('model_retry_backoff_seconds', [0.0, 2.0], tmp_path / 'settings.yaml')]
    assert policy == {'backoff_seconds': [0.0, 2.0], 'source': 'owner', 'cas': 'none'}
    assert user_config_path(api.agent.config) is None or isinstance(user_config_path(api.agent.config), type(tmp_path))


def task_for(api, **updates):
    from agent_py_agent.agent.conversation.session_tasks import SessionTaskDraft
    store = api.agent.conversation_store
    task = store.session_tasks.create(SessionTaskDraft(sender_thread_id=api.tid, target_thread_id=api.tid,
        goal='测试任务'))
    if isinstance(task, tuple):
        task = task[0]
    return task


@pytest.mark.parametrize('source', ['default', 'inherited', 'explicit'])
def test_task_resolution_reads_canonical_selection_without_mutation(w3_api, source):
    from uuid import uuid4

    from agent_py_agent.agent.conversation.models import ConversationThread
    from agent_py_agent.agent.settings.model_profiles import execute_model_profile_operation
    from agent_py_agent.agent.settings.thread_model_selection import (
        initialize_inherited_thread_model,
        thread_model_profile_id,
    )
    from agent_py_agent.tests.test_model_profiles import add

    api = w3_api
    store = api.agent.conversation_store
    profile = add(api.agent, model_name='task-model')[0]
    api.tid = 'task-thread-' + uuid4().hex
    store.threads.write(ConversationThread(api.tid, 'tester', owner_id=api.services.owner_id,
        owner_home=str(api.services.owner_home)))
    if source == 'default':
        execute_model_profile_operation(api.agent, 'set_default', {'profile_id': profile})
        thread_model_profile_id(api.agent, api.tid)
    elif source == 'inherited':
        initialize_inherited_thread_model(api.agent, api.tid, profile)
    else:
        thread_model_profile_id(api.agent, api.tid, select=profile)
    revision = store.threads.load(api.tid).model_selection_revision
    task = task_for(api)
    path = store.storage.thread_path(api.tid)
    before = path.read_bytes()
    result = api.invoke('llm_pool.task_resolution.get', {'task_id': task.task_id})
    assert result['thread_id'] == api.tid and result['model_profile_id'] == profile
    assert result['source'] == source and result['model_selection_revision'] == revision
    assert result['owner_default_profile_id'] == (profile if source == 'default' else 'default')
    assert path.read_bytes() == before


def test_task_override_has_real_model_selection_cas_and_terminal_rejection(w3_api):
    from agent_py_agent.agent.conversation.session_tasks import SessionTaskUpdate
    from agent_py_agent.agent.settings.thread_model_selection import thread_model_profile_id
    from agent_py_agent.tests.test_model_profiles import add
    api = w3_api
    task = task_for(api)
    profile = add(api.agent, model_name='task-override')[0]
    thread = api.agent.conversation_store.threads.load(api.tid)
    params = {'task_id': task.task_id, 'model_profile_id': profile,
        'expected_model_selection_revision': thread.model_selection_revision}
    changed = api.invoke('llm_pool.task_override.set', params)
    assert changed['source'] == 'explicit' and changed['model_profile_id'] == profile
    assert thread_model_profile_id(api.agent, api.tid) == profile
    with pytest.raises(Exception) as error:
        api.invoke('llm_pool.task_override.set', params)
    assert error.value.code == 'REVISION_CONFLICT'
    api.agent.conversation_store.session_tasks.advance(task.task_id, SessionTaskUpdate(status='cancelled'))
    with pytest.raises(Exception) as error:
        api.invoke('llm_pool.task_override.set', {**params, 'expected_model_selection_revision': changed['model_selection_revision']})
    assert error.value.code == 'STATE_CONFLICT'


def test_pool_reset_key_replay_does_not_refill_again(w3_api):
    from uuid import uuid4
    api = w3_api
    rate = TenantRateLimiter(0, 1, clock=lambda: 0)
    api.agent.tenant_rate_limiter = rate
    key = 'reset-replay-' + uuid4().hex
    params = {'scope': 'owner_rate_bucket'}
    first = api.invoke('llm_pool.reset', params, key=key)
    assert rate.allow(api.services.owner_id)
    assert api.invoke('llm_pool.reset', params, key=key) == first
    assert not rate.allow(api.services.owner_id)
    with pytest.raises(Exception) as error:
        api.invoke('llm_pool.reset', {'scope': 'owner_retry_override'}, key=key)
    assert error.value.code == 'IDEMPOTENCY_CONFLICT'


def test_task_override_key_replay_returns_original_revision(w3_api):
    from uuid import uuid4

    from agent_py_agent.tests.test_model_profiles import add
    api = w3_api
    task = task_for(api)
    profile = add(api.agent, model_name='once')[0]
    params = {'task_id': task.task_id, 'model_profile_id': profile,
        'expected_model_selection_revision': api.agent.conversation_store.threads.load(api.tid).model_selection_revision}
    key = 'override-replay-' + uuid4().hex
    first = api.invoke('llm_pool.task_override.set', params, key=key)
    assert api.invoke('llm_pool.task_override.set', params, key=key) == first
    assert api.agent.conversation_store.threads.load(api.tid).model_selection_revision == first['model_selection_revision']


def test_external_effect_without_receipt_is_not_repeated(w3_api, monkeypatch):
    from uuid import uuid4

    from agent_py_agent.agent.business_spaces.v13_store import V13Store
    from agent_py_agent.agent.plugin_host.errors import HostAPIError

    api = w3_api
    rate = TenantRateLimiter(0, 1, clock=lambda: 0)
    api.agent.tenant_rate_limiter = rate
    calls = []
    original_reset = rate.reset
    original_materialized = V13Store._materialized

    def reset(tenant):
        original_reset(tenant)
        calls.append(tenant)

    def fail_after_effect(store, pending):
        if calls:
            raise OSError('receipt publication interrupted')
        return original_materialized(store, pending)

    monkeypatch.setattr(rate, 'reset', reset)
    monkeypatch.setattr(V13Store, '_materialized', fail_after_effect)
    key = 'uncertain-reset-' + uuid4().hex
    params = {'scope': 'owner_rate_bucket'}
    with pytest.raises(HostAPIError) as initial_error:
        api.invoke('llm_pool.reset', params, key=key)
    assert initial_error.value.code == 'OUTCOME_UNKNOWN'
    assert initial_error.value.outcome == 'unknown'
    assert calls == [api.services.owner_id]
    assert rate.allow(api.services.owner_id)
    monkeypatch.setattr(V13Store, '_materialized', original_materialized)
    with pytest.raises(HostAPIError) as error:
        api.invoke('llm_pool.reset', params, key=key)
    assert error.value.code == 'OUTCOME_UNKNOWN'
    assert calls == [api.services.owner_id]
    assert not rate.allow(api.services.owner_id)


def test_client_key_cannot_alias_internal_reservation_receipt(w3_api):
    import hashlib
    from uuid import uuid4

    api = w3_api
    rate = TenantRateLimiter(0, 1, clock=lambda: 0)
    api.agent.tenant_rate_limiter = rate
    key = 'namespace-' + uuid4().hex
    params = {'scope': 'owner_rate_bucket'}
    first = api.invoke('llm_pool.reset', params, key=key)
    assert rate.allow(api.services.owner_id)
    alias = 'reserve-' + hashlib.sha256(key.encode()).hexdigest()
    assert api.invoke('llm_pool.reset', params, key=alias) == first
    assert rate.allow(api.services.owner_id)
