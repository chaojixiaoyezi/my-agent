"""已结束但未结片保责任；日常复核公平有界，已退 worker 不再保正文引用。"""
from __future__ import annotations

import threading
import weakref
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.conversation import background_shutdown as shutdown


class CountingRepo:
    calls = 0
    pending = True

    def __init__(self, *args, **kwargs):
        self.db_path = 'isolated-counting-repository'

    def get_attempt(self, attempt_id):
        type(self).calls += 1
        return {'agent_run_id': 'agent', 'status': 'done', 'ended_at': 1}

    def get_agent_run(self, agent_run_id):
        return {'task_run_id': 'task-run', 'current_attempt_id': 'attempt', 'run_id': 'run', 'status': 'done'}

    def get_task_run(self, task_run_id):
        return {'task_id': 'task', 'status': 'done'}

    def unsettled_attempt_operations(self, attempt_id):
        return [{'status': 'UNKNOWN', 'operation_id': 'exact-operation'}] if self.pending else []

    def get_agent_run_exec_lock(self, agent_run_id):
        return None


def claim(index):
    row = {'schema_version': 'background_run_claim.v1', 'claim_id': f'claim-{index}',
           'thread_id': f'thread-{index}', 'status': 'finished',
           'runtime_authority': {'schema_version': 'conversation_claim_execution.v1', 'claim_id': f'claim-{index}',
                                 'attempt_id': 'attempt', 'agent_run_id': 'agent', 'task_id': 'task', 'run_id': 'run'}}
    return SimpleNamespace(claim_id=row['claim_id'], thread_id=row['thread_id'], claim_scope_id='',
                           claims=SimpleNamespace(load=lambda *a, **kw: dict(row)))


def seed(control, count, monkeypatch):
    monkeypatch.setattr(shutdown, 'RuntimeRepository', CountingRepo)
    monkeypatch.setattr(CountingRepo, 'pending', True)
    for index in range(count):
        with control.observe(CountingRepo()):
            shutdown.observe_background_claim(claim(index))
    CountingRepo.calls = 0


def test_new_observe_rechecks_at_most_eight_of_257_unknown_slices(monkeypatch):
    control = shutdown.BackgroundShutdown()
    seed(control, 257, monkeypatch)
    for _ in range(3):
        before = CountingRepo.calls
        with control.observe(None):
            pass
        assert CountingRepo.calls - before <= 8
    assert len(control.slices) == 257
    before = CountingRepo.calls
    blockers = control.blockers()
    assert CountingRepo.calls - before == 257
    assert len(blockers) == 257
    assert {row['reason'] for row in blockers} == {'background_operation_unsettled'}


def test_fair_recheck_reaches_every_unknown_slice_without_discarding_any(monkeypatch):
    control = shutdown.BackgroundShutdown()
    seed(control, 257, monkeypatch)
    visited = set()
    original = shutdown.BackgroundSlice._claim_blocker
    def count(item, reference):
        visited.add(reference.claim_id)
        return original(item, reference)
    monkeypatch.setattr(shutdown.BackgroundSlice, '_claim_blocker', count)
    for _ in range(33):
        with control.observe(None):
            pass
    assert len(visited) == 257
    assert len(control.slices) == 257


def test_exited_worker_reference_released_but_unknown_claim_still_blocks(monkeypatch):
    control = shutdown.BackgroundShutdown()
    monkeypatch.setattr(shutdown, 'RuntimeRepository', CountingRepo)
    release = threading.Event()
    class Payload:
        pass
    payload = Payload()
    worker = threading.Thread(target=release.wait)
    worker.payload = payload
    payload_ref, worker_ref = weakref.ref(payload), weakref.ref(worker)
    with control.observe(CountingRepo()):
        shutdown.observe_background_claim(claim(0))
        shutdown.start_background_model_worker(worker)
    item, = control.slices
    assert len(item.workers) == 1
    assert any(row['reason'] == 'background_model_worker_unsettled' for row in item.blockers())
    release.set()
    worker.join(5)
    assert not worker.is_alive()
    del worker, payload
    assert worker_ref() is not None
    assert item.blockers() == [{'reason': 'background_operation_unsettled', 'ref': 'claim-0'}]
    assert item.workers == []
    assert item.exited_worker_count == 1
    assert worker_ref() is None and payload_ref() is None
    assert item in control.slices
    assert item.blockers() == [{'reason': 'background_operation_unsettled', 'ref': 'claim-0'}]
    assert item.exited_worker_count == 1


def test_recheck_exception_keeps_entire_reserved_batch_for_later(monkeypatch):
    control = shutdown.BackgroundShutdown()
    seed(control, 17, monkeypatch)
    first = next(iter(control._finished_rechecks))
    original = first.blockers
    error = OSError('isolated exact source')
    def fail():
        raise error
    monkeypatch.setattr(first, 'blockers', fail)
    with pytest.raises(OSError) as caught:
        with control.observe(None):
            pass
    assert caught.value is error
    assert len(control._finished_rechecks) == len(control.slices) == 17
    monkeypatch.setattr(first, 'blockers', original)
    monkeypatch.setattr(CountingRepo, 'pending', False)
    for _ in range(3):
        with control.observe(None):
            pass
    assert not control.slices and not control._finished_rechecks


def test_concurrent_births_reserve_different_batches_without_losing_unknown(monkeypatch):
    control = shutdown.BackgroundShutdown()
    seed(control, 17, monkeypatch)
    first = next(iter(control._finished_rechecks))
    original = first.blockers
    entered, release = threading.Event(), threading.Event()
    def held():
        entered.set()
        assert release.wait(5)
        return original()
    monkeypatch.setattr(first, 'blockers', held)
    errors = []
    def birth():
        try:
            with control.observe(None):
                pass
        except BaseException as exc:
            errors.append(exc)
    worker = threading.Thread(target=birth)
    worker.start()
    try:
        assert entered.wait(5)
        # 第一个出生已预约 8 片；第二个出生须独立推进，而非重复读同一批。
        before = CountingRepo.calls
        with control.observe(None):
            pass
        assert CountingRepo.calls - before == 8
    finally:
        release.set()
        worker.join(5)
    assert not worker.is_alive() and not errors
    assert len(control.slices) == len(control._finished_rechecks) == 17
    assert len(control.blockers()) == 17


def test_worker_identity_uncertainty_never_releases_reference(monkeypatch):
    control = shutdown.BackgroundShutdown()
    seed(control, 0, monkeypatch)
    release = threading.Event()
    worker = threading.Thread(target=release.wait)
    with control.observe(CountingRepo()):
        shutdown.observe_background_claim(claim(0))
        shutdown.start_background_model_worker(worker)
    item, = control.slices
    release.set()
    worker.join(5)
    original_started = worker._started
    worker._started = threading.Event()
    try:
        assert any(row['reason'] == 'background_model_worker_unsettled' for row in item.blockers())
        assert len(item.workers) == 1 and item.exited_worker_count == 0
    finally:
        worker._started = original_started
    assert item.blockers() == [{'reason': 'background_operation_unsettled', 'ref': 'claim-0'}]
    assert not item.workers and item.exited_worker_count == 1
