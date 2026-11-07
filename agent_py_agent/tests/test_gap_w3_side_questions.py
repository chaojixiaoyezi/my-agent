"""旁路提问在真实 owner/Store 上的隔离、取消交错与 V13 合同。"""
from concurrent.futures import CancelledError, Future
from contextvars import ContextVar
from dataclasses import replace
from threading import Event
from types import SimpleNamespace
from uuid import uuid4

import pytest

from agent_py_agent.agent.backends import ModelResponse
from agent_py_agent.agent.concurrency.daemon_executor import DurableDaemonThreadPoolExecutor
from agent_py_agent.agent.gateway_parts import business_api_v13 as v13
from agent_py_agent.agent.plugin_host.errors import HostAPIError
from agent_py_agent.tests.test_gap_w2_intercept_actions import (
    native_owner_runtime as native_owner_runtime,
)


class TrackingPool(DurableDaemonThreadPoolExecutor):
    def __init__(self):
        super().__init__(max_workers=4)
        self.futures = []

    def submit(self, *args, **kwargs):
        future = super().submit(*args, **kwargs)
        self.futures.append(future)
        return future


class HeldPool:
    def __init__(self):
        self.futures = []

    def submit(self, *args, **kwargs):
        future = Future()
        self.futures.append(future)
        return future


def test_native_worker_uses_configured_owner_profile_and_keeps_cancelled_lease(native_owner_runtime, monkeypatch):
    from agent_py_agent.agent.business_spaces import gap158_w3_side_questions as side
    from agent_py_agent.agent.business_spaces.v13_store import V13Store
    from agent_py_agent.agent.plugin_host.model_settings_v2_handlers import ModelSettingsV2Handlers

    runtime = native_owner_runtime
    catalog = ModelSettingsV2Handlers(runtime.host.home_paths).save_provider({'provider_id': 'r1-side',
        'display_name': '侧问回归', 'api_base': 'https://model.invalid/v1', 'api_key': 'test-only-key', 'models': [
            {'model_name': 'r1-side-model', 'model_backend': 'openai_compatible', 'capability': 'agentic'}]})
    profile = catalog['providers'][0]['models'][0]['profile_id']
    entered, release, calls, configs = Event(), Event(), [], []

    class Backend:
        name = 'r1-side-boundary'

        def generate(self, prompt, **kwargs):
            calls.append((prompt, kwargs))
            if len(calls) == 2:
                entered.set()
                assert release.wait(10)
            return ModelResponse(backend=self.name, text='受控回答')

    backend = Backend()

    def configured_backend(kind, config):
        configs.append((kind, config.model_name))
        return backend

    monkeypatch.setattr('agent_py_agent.agent.backends.get_backend', configured_backend)
    pool = TrackingPool()
    monkeypatch.setattr(side, '_executor', pool)
    runtime.host.conversation_store.messages.append({'thread_id': runtime.thread.thread_id,
        'role': 'user', 'content': '来自规范线程的材料'})
    params = {'conversation_id': runtime.thread.thread_id, 'question': '原生侧问', 'model_profile_id': profile}
    owner = (str(runtime.host.home_paths.owner_home_dir.resolve()), runtime.host.home_paths.owner_id)
    try:
        status, body = runtime.call('side_questions.create', params, key='first-side-question')
        assert status == 200 and body['data']['side_question']['state'] == 'queued', body
        first = body['data']['side_question']['request_id']
        pool.futures[0].result(timeout=10)
        row = V13Store(runtime.host.home_paths.owner_home_dir).get('side_questions', first)
        assert row['state'] == 'answered' and row['answer'] == '受控回答'
        assert configs == [('openai_compatible', 'r1-side-model')]
        assert '来自规范线程的材料' in str(calls[0])
        assert not runtime.pool._pins and owner not in side._jobs
        status, body = runtime.call('side_questions.create', params, key='second-side-question')
        assert status == 200, body
        second = body['data']['side_question']['request_id']
        assert entered.wait(5)
        assert runtime.pool._pins.get(id(runtime.host)) == 1 and len(side._jobs[owner]) == 1
        status, body = runtime.call('side_questions.cancel', {'request_id': second}, key='cancel-side-question')
        assert status == 200 and body['data']['state'] == 'cancelled', body
        assert not pool.futures[1].done()
        assert runtime.pool._pins.get(id(runtime.host)) == 1 and second in side._jobs[owner]
        release.set()
        pool.futures[1].result(timeout=10)
        row = V13Store(runtime.host.home_paths.owner_home_dir).get('side_questions', second)
        assert row['state'] == 'cancelled' and not row.get('answer')
        assert not runtime.pool._pins and owner not in side._jobs
        assert [kind for kind, _ in runtime.leases] == ['acquired', 'released'] * 2
    finally:
        release.set()
        pool.shutdown(wait=True)


def test_native_side_worker_acquisition_failure_releases_job_slot(native_owner_runtime, monkeypatch):
    from agent_py_agent.agent.business_spaces import gap158_w3_side_questions as side
    from agent_py_agent.agent.business_spaces.v13_store import V13Store

    runtime = native_owner_runtime
    pool = TrackingPool()
    monkeypatch.setattr(side, '_executor', pool)

    def unavailable(*args, **kwargs):
        raise RuntimeError('owner runtime unavailable')

    monkeypatch.setattr(runtime.pool, 'acquire_pinned', unavailable)
    try:
        status, body = runtime.call('side_questions.create', {'conversation_id': runtime.thread.thread_id,
            'question': '宿主不可用'}, key='unavailable-side-question')
        assert status == 200, body
        pool.futures[0].result(timeout=10)
        row = V13Store(runtime.host.home_paths.owner_home_dir).get('side_questions',
            body['data']['side_question']['request_id'])
        assert row['state'] == 'failed' and row['error_code'] == 'HOST_NOT_READY'
        assert not runtime.pool._pins and not side._jobs
    finally:
        pool.shutdown(wait=True)


@pytest.fixture
def w3_api(tmp_path, monkeypatch):
    from agent_py_agent.agent.business_spaces import gap158_w3_side_questions as side
    from agent_py_agent.agent.core import SimpleAgent
    from agent_py_agent.agent.gateway_parts import business_api as business
    from agent_py_agent.agent.gateway_parts import business_api_v13_gap158_w3 as slice_module
    from agent_py_agent.agent.settings.config import AgentConfig

    assert len(getattr(slice_module, 'V13_ACTIONS', ())) == 11, 'W3 must export all contracted actions'
    for definition in slice_module.V13_ACTIONS:
        monkeypatch.setitem(v13._ACTIONS, definition.name, definition)
    for name, shape in slice_module.V13_DEFS.items():
        monkeypatch.setitem(v13.V13_DEFS, name, shape)
    monkeypatch.setitem(v13.COLLECTIONS, 'side_questions', 'owner')
    agent = SimpleAgent(AgentConfig(my_agent_home=str(tmp_path / 'home'), model_backend='echo',
        enable_tools=False, business_spaces_enabled=True), tmp_path / 'workspace')
    agent.config.side_questions_enabled = True
    # 默认目录不配置外部连接；后端替身仍走正式辅助调用的记账与准入。
    monkeypatch.setattr('agent_py_agent.agent.backends.get_backend',
        lambda *_: SimpleNamespace(name='fake', generate=lambda *a, **k: ModelResponse(backend='fake', text='旁路回答')))
    paths = agent.home_paths
    context = business.bind_business_context(agent, authorize=lambda *_: True)
    services = SimpleNamespace(host=agent, owner_id=paths.owner_id, owner_home=paths.owner_home_dir)
    pool = TrackingPool()
    monkeypatch.setattr(side, '_executor', pool)
    thread = agent.conversation_store.threads.get_or_create({'canonical_user_id': 'tester', 'channel': 'chat',
        'channel_conversation_id': 'w3-test', 'channel_user_id': 'tester', 'owner_id': paths.owner_id,
        'owner_home': str(paths.owner_home_dir)})

    def invoke(action, params=None, *, key=None, revision=None):
        return v13.v13_invoke(action, params or {}, context, services,
            request_id='request-' + uuid4().hex, idempotency_key=key or 'operation-' + uuid4().hex,
            expected_revision=revision)

    api = SimpleNamespace(agent=agent, invoke=invoke, side=side, pool=pool, tid=thread.thread_id,
        services=services, context=context)
    try:
        yield api
    finally:
        for future in pool.futures:
            try:
                future.result(timeout=5)
            except CancelledError:
                pass
        pool.shutdown(wait=True)


def create(api, **extra):
    return api.invoke('side_questions.create', {'conversation_id': api.tid, 'question': '旁路问题', **extra})['side_question']


def finish(api):
    for future in api.pool.futures:
        future.result(timeout=5)


def test_answer_never_changes_active_main_history_or_thread(w3_api):
    api = w3_api
    store = api.agent.conversation_store
    store.messages.append({'thread_id': api.tid, 'role': 'user', 'content': '主会话材料'})
    store.threads.update_atomic(api.tid, lambda row: replace(row, active_task_ids=['main-active'],
        summary='压缩摘要', compact_generation=3))
    paths = (store.storage.message_path(api.tid), store.storage.thread_path(api.tid))
    before = [(path.stat().st_size, path.read_bytes()) for path in paths]
    row = create(api)
    assert row['state'] == 'queued'
    finish(api)
    page = api.invoke('side_questions.list', {'conversation_id': api.tid})
    assert page['items'][0]['state'] == 'answered'
    assert page['items'][0]['answer'] == '旁路回答'
    assert [(path.stat().st_size, path.read_bytes()) for path in paths] == before
    events = api.invoke('side_questions.events', {'request_id': row['request_id']})
    assert [item['kind'] for item in events['events']] == ['started', 'answered']
    assert not events['overflow']
    with pytest.raises(Exception) as error:
        api.invoke('side_questions.delete', {'side_question_id': row['side_question_id']}, revision=0)
    assert error.value.code == 'REVISION_CONFLICT'
    assert api.invoke('side_questions.delete', {'side_question_id': row['side_question_id']},
        revision=page['revision'])['deleted'] is True


def test_cancel_during_model_discards_late_result_and_keeps_slot(w3_api, monkeypatch):
    api = w3_api
    entered, release = Event(), Event()

    def generate(request):
        entered.set()
        assert release.wait(5)
        return ModelResponse(backend='fake', text='迟到结果')

    monkeypatch.setattr(api.side, 'generate_auxiliary_model_response', generate)
    row = create(api)
    try:
        assert entered.wait(5)
        with pytest.raises(Exception) as error:
            api.invoke('side_questions.delete', {'side_question_id': row['side_question_id']}, revision=2)
        assert error.value.code == 'STATE_CONFLICT'
        cancelled = api.invoke('side_questions.cancel', {'request_id': row['request_id']})
        assert cancelled['state'] == 'cancelled'
    finally:
        release.set()
    finish(api)
    final = api.invoke('side_questions.list', {'conversation_id': api.tid})['items'][0]
    assert final['state'] == 'cancelled' and not final.get('answer')
    assert api.invoke('side_questions.cancel', {'request_id': row['request_id']})['state'] == 'cancelled'


def test_cancel_before_worker_start_never_calls_model(w3_api, monkeypatch):
    api = w3_api
    held = HeldPool()
    monkeypatch.setattr(api.side, '_executor', held)
    monkeypatch.setattr(api.side, 'generate_auxiliary_model_response',
        lambda _: pytest.fail('cancelled queued work must not call model'))
    row = create(api)
    assert api.invoke('side_questions.cancel', {'request_id': row['request_id']})['state'] == 'cancelled'
    assert held.futures[0].cancelled()


def test_terminal_cancel_is_noop_and_create_replay_does_not_resubmit(w3_api):
    api = w3_api
    params = {'conversation_id': api.tid, 'question': '原问题'}
    key = 'stable-' + uuid4().hex
    first = api.invoke('side_questions.create', params, key=key)
    finish(api)
    assert api.invoke('side_questions.create', params, key=key) == first
    assert len(api.pool.futures) == 1
    rid = first['side_question']['request_id']
    assert api.invoke('side_questions.cancel', {'request_id': rid})['state'] == 'answered'
    with pytest.raises(Exception) as error:
        api.invoke('side_questions.create', {**params, 'question': '不同问题'}, key=key)
    assert error.value.code == 'IDEMPOTENCY_CONFLICT'


def test_concurrency_cap_owner_isolation_and_switch(w3_api, monkeypatch):
    api = w3_api
    held = HeldPool()
    monkeypatch.setattr(api.side, '_executor', held)
    rows = [create(api) for _ in range(api.side._limits.SIDE_QUESTIONS_MAX_CONCURRENT_PER_OWNER)]
    with pytest.raises(HostAPIError) as error:
        create(api)
    assert error.value.code == 'LIMIT_EXCEEDED'
    foreign = api.agent.conversation_store.threads.get_or_create({'canonical_user_id': 'foreign', 'channel': 'chat',
        'channel_conversation_id': 'foreign', 'channel_user_id': 'foreign', 'owner_id': 'foreign'})
    with pytest.raises(HostAPIError) as error:
        create(api, conversation_id=foreign.thread_id)
    assert error.value.code == 'RESOURCE_NOT_FOUND'
    with pytest.raises(HostAPIError) as error:
        api.invoke('side_questions.events', {'request_id': 'unknown-request'})
    assert error.value.code == 'RESOURCE_NOT_FOUND'
    api.agent.config.side_questions_enabled = False
    assert not v13.v13_ready('side_questions.create', api.services, api.context)
    with pytest.raises(HostAPIError) as error:
        create(api)
    assert error.value.code == 'HOST_NOT_READY'
    api.agent.config.side_questions_enabled = True
    for row in rows:
        api.invoke('side_questions.cancel', {'request_id': row['request_id']})


def test_bounded_pagination_pruning_and_ring_restart_overflow(w3_api, monkeypatch):
    api = w3_api
    monkeypatch.setattr(api.side, '_limits', SimpleNamespace(**{**vars(api.side._limits),
        'SIDE_QUESTIONS_MAX_PER_CONVERSATION': 2, 'SIDE_QUESTION_EVENT_RING': 1}))
    first = create(api)
    finish(api)
    events = api.invoke('side_questions.events', {'request_id': first['request_id'], 'after_seq': 0})
    assert events['overflow'] is True and len(events['events']) == 1
    create(api)
    finish(api)
    page = api.invoke('side_questions.list', {'conversation_id': api.tid, 'limit': 1})
    assert len(page['items']) == 1 and page['next_cursor'] and page['coverage'] == 'partial'
    last = api.invoke('side_questions.list', {'conversation_id': api.tid, 'limit': 1, 'cursor': page['next_cursor']})
    assert len(last['items']) == 1 and last['items'] != page['items']
    create(api)
    finish(api)
    result = api.invoke('side_questions.list', {'conversation_id': api.tid})
    assert len(result['items']) == 2 and first['side_question_id'] not in {r['side_question_id'] for r in result['items']}
    with pytest.raises(Exception) as error:
        api.invoke('side_questions.list', {'conversation_id': api.tid, 'cursor': page['next_cursor']})
    assert error.value.code == 'CURSOR_EXPIRED'
    api.side._events.clear()
    event = api.invoke('side_questions.events', {'request_id': result['items'][0]['request_id']})
    assert event['overflow'] is True and event['events'] == [] and event['state'] == 'answered'


def test_fresh_context_and_explicit_profile_do_not_leak(w3_api, monkeypatch):
    from agent_py_agent.agent.settings.model_scope import _BINDINGS, selected_model_scope
    from agent_py_agent.agent.settings.thread_model_selection import thread_model_profile_id
    from agent_py_agent.tests.test_model_profiles import add

    api = w3_api
    marker = ContextVar('w3-caller-marker', default='fresh')
    marker.set('caller')
    a = add(api.agent, model_name='A')[0]
    b = add(api.agent, model_name='B')[0]
    thread_model_profile_id(api.agent, api.tid, select=a)
    observed = []

    def generate(request):
        observed.append((marker.get(), api.agent.config.model_name, request.purpose, request.thread_id))
        return ModelResponse(backend='fake', text='隔离回答')

    monkeypatch.setattr(api.side, 'generate_auxiliary_model_response', generate)
    thread_bytes = api.agent.conversation_store.storage.thread_path(api.tid).read_bytes()
    with selected_model_scope(api.agent, thread_id=api.tid):
        binding = _BINDINGS.get()
        create(api, model_profile_id=b)
        finish(api)
        assert _BINDINGS.get() is binding and api.agent.config.model_name == 'A'
    assert observed == [('fresh', 'B', 'side_question', api.tid)]
    assert api.agent.conversation_store.storage.thread_path(api.tid).read_bytes() == thread_bytes


def test_auxiliary_backend_accepts_side_question(w3_api):
    from agent_py_agent.agent.conversation.auxiliary_model_call import (
        AuxiliaryModelCallRequest,
        generate_auxiliary_model_response,
    )
    from agent_py_agent.agent.settings.model_scope import selected_model_scope

    api = w3_api
    with selected_model_scope(api.agent, thread_id=api.tid, active=False, initialize_thread=False):
        result = generate_auxiliary_model_response(AuxiliaryModelCallRequest(agent=api.agent, prompt='问题',
            thread_id=api.tid, purpose='side_question', request_id='side-diagnostic'))
    assert result.text == '旁路回答'


def test_authenticated_http_roundtrip_and_foreign_request_ids(tmp_path, monkeypatch):
    from agent_py_agent.agent.gateway_parts import business_api_v13_gap158_w3 as slice_module
    from agent_py_agent.tests.test_v13_w0_harness import v13_call, v13_native

    # W0 未合时仅补注册表夹具；路由、owner 解析和标准信封都是真实实现。
    for definition in slice_module.V13_ACTIONS:
        monkeypatch.setitem(v13._ACTIONS, definition.name, definition)
    for name, shape in slice_module.V13_DEFS.items():
        monkeypatch.setitem(v13.V13_DEFS, name, shape)
    monkeypatch.setitem(v13.COLLECTIONS, 'side_questions', 'owner')
    generator = v13_native.__wrapped__(tmp_path, monkeypatch)
    native = next(generator)
    server, agent = native
    agent.config.side_questions_enabled = True
    # 首次请求建立普通 owner 后只启用该 owner 的显式测试开关。
    try:
        status, _ = v13_call(native, 'llm_pool.retry_policy.get', {}, user='business-alice')
        assert status == 200
        services = next(value for value in server._business_services.values()
            if value.owner_id.endswith('/business-alice'))
        services.host.config.side_questions_enabled = True
        store = services.host.conversation_store
        thread = store.threads.get_or_create({'canonical_user_id': 'business-alice', 'channel': 'chat',
            'channel_conversation_id': 'http-w3', 'channel_user_id': 'business-alice',
            'owner_id': services.owner_id, 'owner_home': str(services.owner_home)})
        held = HeldPool()
        from agent_py_agent.agent.business_spaces import gap158_w3_side_questions as side
        monkeypatch.setattr(side, '_executor', held)
        status, accepted = v13_call(native, 'side_questions.create', {'conversation_id': thread.thread_id,
            'question': '真实 HTTP 旁路'}, user='business-alice', idempotency_key='http-side-' + uuid4().hex)
        assert status == 200 and accepted['error'] is None, accepted
        rid = accepted['data']['side_question']['request_id']
        status, hidden = v13_call(native, 'side_questions.events', {'request_id': rid}, user='business-bob')
        if status == 503:
            bob = next(value for value in server._business_services.values() if value.owner_id.endswith('/business-bob'))
            bob.host.config.side_questions_enabled = True
            status, hidden = v13_call(native, 'side_questions.events', {'request_id': rid}, user='business-bob')
        assert status == 404 and hidden['error']['code'] == 'RESOURCE_NOT_FOUND'
        status, cancelled = v13_call(native, 'side_questions.cancel', {'request_id': rid}, user='business-alice',
            idempotency_key='http-cancel-' + uuid4().hex)
        assert status == 200 and cancelled['data']['state'] == 'cancelled', cancelled
        assert held.futures[0].cancelled()
    finally:
        try:
            next(generator)
        except StopIteration:
            pass


def test_context_tail_obeys_both_message_and_character_bounds(w3_api, monkeypatch):
    api = w3_api
    store = api.agent.conversation_store
    for value in ('A' * 10, 'B' * 10, 'C' * 10):
        store.messages.append({'thread_id': api.tid, 'role': 'user', 'content': value})
    monkeypatch.setattr(api.side, '_limits', SimpleNamespace(**{**vars(api.side._limits),
        'SIDE_QUESTION_CONTEXT_MAX_MESSAGES': 2, 'SIDE_QUESTION_CONTEXT_MAX_CHARS': 15}))
    observed = []

    def generate(request):
        observed.append(request.messages)
        return ModelResponse(backend='fake', text='bounded')

    monkeypatch.setattr(api.side, 'generate_auxiliary_model_response', generate)
    create(api)
    finish(api)
    assert observed == [[{'role': 'user', 'content': 'B' * 5}, {'role': 'user', 'content': 'C' * 10}]]


def test_delete_replay_returns_original_receipt(w3_api):
    api = w3_api
    row = create(api)
    finish(api)
    revision = api.invoke('side_questions.list', {'conversation_id': api.tid})['revision']
    key = 'delete-replay-' + uuid4().hex
    params = {'side_question_id': row['side_question_id']}
    first = api.invoke('side_questions.delete', params, key=key, revision=revision)
    assert api.invoke('side_questions.delete', params, key=key, revision=revision) == first


def test_cancel_uses_current_actor_after_creator_revocation(w3_api, monkeypatch):
    from agent_py_agent.agent.gateway_parts.business_api import bind_business_context

    api = w3_api
    entered, release = Event(), Event()
    revoked = {'value': False}
    object.__setattr__(api.context, 'authorize', lambda action, facts: not (
        revoked['value'] and action == 'side_questions.create'))

    def generate(request):
        entered.set()
        assert release.wait(5)
        return ModelResponse(backend='fake', text='late')

    monkeypatch.setattr(api.side, 'generate_auxiliary_model_response', generate)
    row = create(api)
    try:
        assert entered.wait(5)
        revoked['value'] = True
        actor_b = bind_business_context(api.agent, authorize=lambda *_: True, subject_id='actor-b')
        result = v13.v13_invoke('side_questions.cancel', {'request_id': row['request_id']}, actor_b,
            api.services, request_id='cancel-new-actor', idempotency_key='new-actor-' + uuid4().hex)
        assert result['state'] == 'cancelled'
    finally:
        release.set()
    finish(api)
    assert api.invoke('side_questions.events', {'request_id': row['request_id']})['state'] == 'cancelled'


def test_explicit_profile_works_with_deleted_canonical_profile(w3_api, monkeypatch):
    from agent_py_agent.agent.settings.model_profiles import execute_model_profile_operation
    from agent_py_agent.agent.settings.thread_model_selection import thread_model_profile_id
    from agent_py_agent.tests.test_model_profiles import add

    api = w3_api
    original, explicit = add(api.agent, model_name='deleted')[0], add(api.agent, model_name='valid')[0]
    thread_model_profile_id(api.agent, api.tid, select=original)
    execute_model_profile_operation(api.agent, 'delete_model', {'profile_id': original})
    observed = []

    def generate(request):
        observed.append(api.agent.config.model_name)
        return ModelResponse(backend='fake', text='valid answer')

    monkeypatch.setattr(api.side, 'generate_auxiliary_model_response', generate)
    before = api.agent.conversation_store.storage.thread_path(api.tid).read_bytes()
    create(api, model_profile_id=explicit)
    finish(api)
    assert observed == ['valid']
    assert api.invoke('side_questions.list', {'conversation_id': api.tid})['items'][0]['state'] == 'answered'
    assert api.agent.conversation_store.storage.thread_path(api.tid).read_bytes() == before


def test_queued_event_cache_is_bounded_on_create(w3_api, monkeypatch):
    api = w3_api
    monkeypatch.setattr(api.side, '_executor', HeldPool())
    # 只改变观察缓存容量的派生输入，不增加 owner 并发容量。
    monkeypatch.setattr(api.side, '_limits', SimpleNamespace(**{**vars(api.side._limits),
        'SIDE_QUESTIONS_MAX_PER_CONVERSATION': 1, 'SIDE_QUESTION_WORKERS': 1}))
    api.side._events.clear()
    first = create(api)
    thread = api.agent.conversation_store.threads.get_or_create({'canonical_user_id': 'second', 'channel': 'chat',
        'channel_conversation_id': 'second', 'channel_user_id': 'second', 'owner_id': api.services.owner_id,
        'owner_home': str(api.services.owner_home)})
    second = create(api, conversation_id=thread.thread_id)
    assert len(api.side._events) <= 1
    for row in (first, second):
        api.invoke('side_questions.cancel', {'request_id': row['request_id']})


def test_cancel_idempotency_key_cannot_target_a_different_request(w3_api, monkeypatch):
    api = w3_api
    monkeypatch.setattr(api.side, '_executor', HeldPool())
    first, second = create(api), create(api)
    key = 'cancel-replay-' + uuid4().hex
    params = {'request_id': first['request_id']}
    try:
        original = api.invoke('side_questions.cancel', params, key=key)
        assert api.invoke('side_questions.cancel', params, key=key) == original
        with pytest.raises(Exception) as error:
            api.invoke('side_questions.cancel', {'request_id': second['request_id']}, key=key)
        assert error.value.code == 'IDEMPOTENCY_CONFLICT'
    finally:
        api.invoke('side_questions.cancel', {'request_id': second['request_id']})


def test_client_create_key_cannot_alias_internal_transition(w3_api):
    api = w3_api
    first = create(api)
    finish(api)
    alias = first['side_question_id'] + '-running'
    second = api.invoke('side_questions.create', {'conversation_id': api.tid, 'question': '另一个合法请求'}, key=alias)
    finish(api)
    assert second['side_question']['side_question_id'] != first['side_question_id']
    assert len(api.pool.futures) == 2
