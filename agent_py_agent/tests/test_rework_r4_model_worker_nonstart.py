"""R4-R1：真实 CPython 启动拒绝不能遗留不存在 worker 的保留权。"""
from contextlib import nullcontext
from threading import Event, Thread, current_thread
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from agent_py_agent.agent.agent_core import tool_model_generation as generation
from agent_py_agent.agent.concurrency import interrupt
from agent_py_agent.agent.contracts.model_call_ledger import (
    ModelCallFailureParams,
    ModelCallLedger,
    model_call_drain_snapshot,
)
from agent_py_agent.agent.conversation import background_shutdown as shutdown
from agent_py_agent.tests.test_mega_background_lifecycle_controls import native_cancellation_case
from agent_py_agent.tests.test_rework_r3_model_worker_cancellation import (
    _guard_request,
    _guard_state,
    _retentions,
    _started_call,
)


# LLM: 仍执行原 Thread.start 和 CPython C spawn；非 callable bootstrap 被 C 入口拒绝，未替换 start 或伪造死亡。
# 函数用途: 构造一个可确定无 native worker 的原生启动故障，测试最终只清这个准确合成调用。
def _refusing_thread(**kwargs):
    worker = Thread(**kwargs)
    worker._bootstrap = None
    return worker


@pytest.mark.parametrize('missing_native', [False, True])
def test_native_spawn_site_accepts_cpython_keyword_call_instruction(monkeypatch, missing_native):
    name, native, _offset = interrupt._NATIVE_SPAWN_SITE
    instructions = [SimpleNamespace(opname='LOAD_GLOBAL', argval=name),
                    SimpleNamespace(opname='CALL_KW', offset=101),
                    SimpleNamespace(opname='CALL', offset=202)]
    with monkeypatch.context() as patch:
        patch.setattr(interrupt.dis, 'get_instructions', lambda _code: iter(instructions))
        if missing_native:
            patch.setattr(interrupt._thread, name[1:], None)
        site = interrupt._native_spawn_site()
    assert site == (None if missing_native else (name, native, 101)), 'unknown native entry or keyword CALL cannot use the later ACK wait'


def test_original_native_spawn_refusal_is_proven_and_bound_to_original_identity():
    worker = _refusing_thread(target=lambda: pytest.fail('native refusal must not invoke a worker'))
    observation = interrupt._ThreadExitObservation(worker)
    with pytest.raises(TypeError):
        observation.start()
    assert observation.attempted
    assert observation.exited(), 'native spawn refusal has no physical exit still to wait for'
    assert observation.proven_not_started()
    decoy = Thread()
    observation.thread, observation.started = decoy, decoy._started
    assert not observation.proven_not_started() and not observation.exited()


def test_nonstart_fact_is_established_under_original_runtime_ownership_lock(monkeypatch):
    original = interrupt._ThreadExitObservation.__setattr__
    writes = []

    def assign(observation, name, value):
        if name == '_nonstart_identity' and value is not None:
            assert interrupt._NATIVE_THREAD_OWNERSHIP[0]._is_owned(), 'proof must be established under runtime ownership lock'
            writes.append(value)
        return original(observation, name, value)

    monkeypatch.setattr(interrupt._ThreadExitObservation, '__setattr__', assign)
    observation = interrupt._ThreadExitObservation(_refusing_thread(target=lambda: None))
    with pytest.raises(TypeError):
        observation.start()
    assert writes == [observation._identity] and observation.proven_not_started()


@pytest.mark.parametrize('observed', [False, True])
def test_nonstart_callback_runs_once_without_masking_original_exception(observed):
    manager = shutdown.BackgroundShutdown()
    callback = Mock(side_effect=RuntimeError('secondary callback failure'))
    with manager.observe(None) if observed else nullcontext():
        with pytest.raises(TypeError):
            shutdown.start_background_model_worker(_refusing_thread(target=lambda: None), on_proven_not_started=callback)
    callback.assert_called_once_with()
    assert manager.blockers() == [] and manager.slices == set()


@pytest.mark.parametrize('observed', [False, True])
@pytest.mark.parametrize('replacement', ['mock', 'function'])
def test_replaced_start_without_birth_remains_unknown(monkeypatch, observed, replacement):
    error = RuntimeError('untrusted start has no birth evidence')
    callback = Mock()
    manager = shutdown.BackgroundShutdown()
    observations = []
    original = interrupt._ThreadExitObservation

    def observe(worker):
        observation = original(worker)
        observations.append(observation)
        return observation

    def start(_worker):
        raise error

    monkeypatch.setattr(shutdown, '_ThreadExitObservation', observe)
    monkeypatch.setattr(Thread, 'start', Mock(side_effect=error) if replacement == 'mock' else start)
    with manager.observe(None) if observed else nullcontext():
        with pytest.raises(RuntimeError) as caught:
            shutdown.start_background_model_worker(Thread(), on_proven_not_started=callback)
    assert caught.value is error
    assert observations[0].attempted and not observations[0].proven_not_started()
    assert not observations[0].exited()
    callback.assert_not_called()
    assert bool(manager.blockers()) is observed


@pytest.mark.parametrize('observed', [False, True])
def test_native_birth_with_lost_ack_stays_retained_until_original_worker_exit(monkeypatch, observed):
    ledger = ModelCallLedger()
    _started_call(ledger, 'guard-call')
    born, release = Event(), Event()
    workers = []
    error = RuntimeError('original native worker acknowledgement lost')
    manager = shutdown.BackgroundShutdown()
    provider = Mock(return_value='late result')

    def worker_factory(**kwargs):
        worker = Thread(**kwargs)
        original_bootstrap, original_wait = worker._bootstrap_inner, worker._started.wait

        def bootstrap():
            born.set()
            assert release.wait(10)
            original_bootstrap()

        def wait(timeout=None):
            if not release.is_set():
                assert born.wait(5)
                raise error
            return original_wait(timeout)

        worker._bootstrap_inner, worker._started.wait = bootstrap, wait
        workers.append(worker)
        return worker

    monkeypatch.setattr(generation, 'Thread', worker_factory)
    monkeypatch.setattr(generation, '_generate_backend_response', provider)
    try:
        with manager.observe(None) if observed else nullcontext():
            with pytest.raises(RuntimeError) as caught:
                generation._generate_through_wall_guard(_guard_request(), _guard_state(ledger))
        assert caught.value is error and born.is_set()
        ledger.failed(ModelCallFailureParams(call_id='guard-call', error_type='logical_failure'))
        assert workers[0].ident is None and not workers[0]._started.is_set()
        assert _retentions(ledger, 'guard-call')
        provider.assert_not_called()
        assert bool(manager.blockers()) is observed
    finally:
        release.set()
        if workers:
            assert workers[0]._started.wait(5)
            workers[0].join(timeout=5)
    provider.assert_called_once()
    assert not workers[0].is_alive() and not _retentions(ledger, 'guard-call')
    assert manager.blockers() == []


@pytest.mark.parametrize('observed', [False, True])
def test_native_refusal_releases_exact_failed_call_and_allows_a_healthy_call(monkeypatch, observed):
    ledger = ModelCallLedger()
    _started_call(ledger, 'guard-call')
    _started_call(ledger, 'other-call')
    unrelated = ledger.retain_call('other-call')
    provider = Mock(return_value='healthy result')
    manager = shutdown.BackgroundShutdown()
    monkeypatch.setattr(generation, '_generate_backend_response', provider)
    try:
        with monkeypatch.context() as patch:
            patch.setattr(generation, 'Thread', _refusing_thread)
            with manager.observe(None) if observed else nullcontext():
                with pytest.raises(TypeError) as caught:
                    generation._generate_through_wall_guard(_guard_request(), _guard_state(ledger))
                ledger.failed(ModelCallFailureParams(call_id='guard-call', error_type=type(caught.value).__name__))
        provider.assert_not_called()
        assert not _retentions(ledger, 'guard-call')
        assert _retentions(ledger, 'other-call') == [unrelated]
        assert manager.blockers() == [] and manager.slices == set()
        workers = []

        def healthy(*_args):
            workers.append(current_thread())
            return provider()

        _started_call(ledger, 'healthy-call')
        monkeypatch.setattr(generation, '_generate_backend_response', healthy)
        assert generation._generate_through_wall_guard(_guard_request(), _guard_state(ledger, 'healthy-call')) == 'healthy result'
        workers[0].join(timeout=5)
        assert not workers[0].is_alive() and not _retentions(ledger, 'healthy-call')
        provider.assert_called_once()
    finally:
        # 故障注入器已知 C spawn 从未接受 bootstrap；只回收红灯版本遗留的该准确 token。
        for token in _retentions(ledger, 'guard-call'):
            token.release()
        unrelated.release()


@pytest.mark.parametrize('observed', [False, True])
def test_observer_preparation_failure_releases_guard_token_without_attempting_start(monkeypatch, observed):
    ledger = ModelCallLedger()
    _started_call(ledger, 'guard-call')
    failure = RuntimeError('original observation preparation failure')
    start = Mock(side_effect=AssertionError('preparation failed before Thread.start'))
    provider = Mock()
    manager = shutdown.BackgroundShutdown()
    monkeypatch.setattr(generation, '_generate_backend_response', provider)
    monkeypatch.setattr(shutdown, '_ThreadExitObservation', Mock(side_effect=failure))
    monkeypatch.setattr(Thread, 'start', start)
    try:
        with manager.observe(None) if observed else nullcontext():
            with pytest.raises(RuntimeError) as caught:
                generation._generate_through_wall_guard(_guard_request(), _guard_state(ledger))
        assert caught.value is failure
        start.assert_not_called()
        provider.assert_not_called()
        assert not _retentions(ledger, 'guard-call')
        assert manager.blockers() == []
    finally:
        for token in _retentions(ledger, 'guard-call'):
            token.release()


@pytest.mark.parametrize('observed', [False, True])
def test_original_failed_run_and_attempt_cancel_after_native_nonstart(tmp_path, monkeypatch, observed):
    agent, backend, scheduler, call, task_id, _view = native_cancellation_case(tmp_path, request_timeout=240)
    store, repo = agent.conversation_store, agent.subagents.runtime_db
    manager = shutdown.BackgroundShutdown()
    with monkeypatch.context() as patch:
        patch.setattr(generation, 'Thread', _refusing_thread)
        with manager.observe(repo) if observed else nullcontext():
            with pytest.raises(TypeError):
                scheduler.tick()
    task = store.session_tasks.require(task_id)
    authority = store.claims.load(task.target_thread_id)['runtime_authority']
    original = [(source, row, tokens) for source, row, tokens in model_call_drain_snapshot()
        if row.run_id == authority['run_id'] and row.metadata.get('attempt_id') == authority['attempt_id']]
    try:
        assert agent.config.request_timeout == 240 and backend.calls == 0
        assert repo.get_attempt(authority['attempt_id'])['status'] == 'failed'
        assert repo.get_agent_run_exec_lock(authority['agent_run_id']) is None
        assert original == [], 'failed logical attempt cannot leave a nonexistent native worker retained'
        assert manager.blockers() == [] and manager.slices == set()
        params = {'run_id': authority['run_id'], 'attempt_id': authority['attempt_id'], 'include_descendants': True}
        assert call('executions.cancel', params)['state'] == 'completed'
        assert call('tasks.cancel', {'task_id': task_id, 'include_descendants': True})['state'] == 'completed'
    finally:
        for _source, _row, tokens in original:
            for token in tokens:
                token.release()
