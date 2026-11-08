"""只回收无订阅的空 owner；真实池验证激活水位、作业 Context 与请求引用释放。"""
from __future__ import annotations

import gc
import threading
import weakref
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from contextvars import ContextVar

import pytest

from agent_py_agent.agent.plugin_events.protocol import EventFact
from agent_py_agent.tests.test_plugin_event_hub import _Harness, _installation, _owner


@contextmanager
def _case(**kwargs):
    harness = _Harness(**kwargs)
    try:
        yield harness
    finally:
        harness.hub.close()
        harness.executor.shutdown(wait=True)
        harness.pool.close()


def _assert_empty(hub):
    with hub._lock:
        for name in ('_owners', '_latest', '_slots', '_version', '_managed', '_stats', '_running', '_inflight'):
            assert not getattr(hub, name), name


@pytest.mark.parametrize('subscription', ['empty', 'disabled', 'inactive', 'no-events'])
def test_two_thousand_never_subscribed_owners_are_reclaimed(tmp_path, subscription):
    row = _installation()
    if subscription == 'disabled':
        row.enabled = False
    elif subscription == 'inactive':
        row.activation = None
    elif subscription == 'no-events':
        row.manifest.events = ()
    with _case(rows=[] if subscription == 'empty' else [row]) as h:
        for number in range(2000):
            h.hub.publish(_owner(tmp_path / f'owner-{number}'),
                EventFact(type='prompt_submitted', content='x' * 4000))
            h.drain()
        _assert_empty(h.hub)
        assert not h.plugins


@pytest.mark.parametrize('unreadable', [False, True], ids=['unknown', 'read-error'])
def test_unknown_installations_retain_pending_owner_until_authoritative_empty(unreadable):
    with _case(rows=None) as h:
        if unreadable:
            def fail(_owner):
                raise OSError('installation table unavailable')

            h.hub._installations = fail
        h.publish('prompt_submitted', content='pending')
        h.drain()
        key = str(_owner().home_dir)
        with h.hub._lock:
            assert h.hub._owners[key].home_dir == key
            assert h.hub._latest[key]['prompt_submitted'].fact.content == 'pending'
            assert h.hub._version[key] == 1 and not h.hub._running
        h.hub._installations = lambda _owner: ()
        h.publish('prompt_submitted', content='next')
        h.drain()
        _assert_empty(h.hub)


def _run_next(executor, errors):
    try:
        function, args = executor.jobs.pop(0)
        function(*args)
    except BaseException as error:
        errors.append(error)


@pytest.mark.parametrize('close_during_read', [False, True], ids=['publish', 'close'])
def test_publish_during_empty_table_read_rechecks_before_retirement(close_during_read):
    entered, release = threading.Event(), threading.Event()
    errors, reads = [], []
    with _case(rows=[]) as h:
        def installations(_owner):
            snapshot = tuple(h.rows)
            reads.append(snapshot)
            if len(reads) == 1:
                entered.set()
                assert release.wait(5)
            return snapshot

        h.hub._installations = installations
        h.publish('prompt_submitted', content='before subscription')
        worker = threading.Thread(target=_run_next, args=(h.executor, errors))
        worker.start()
        try:
            assert entered.wait(5)
            if close_during_read:
                h.hub.close()
            else:
                h.rows = [_installation(event_types=('prompt_submitted',), content='text')]
                h.publish('prompt_submitted', content='newest')
        finally:
            release.set()
            worker.join(5)
        assert not worker.is_alive() and not errors
        if close_during_read:
            _assert_empty(h.hub)
            assert not h.plugins
            return
        h.drain()
        assert len(reads) == 2 and h.total_calls == 1
        event = h.events()[0]
        assert (event['content'], event['seq'], event['dropped_before']) == ('newest', 1, 0)
        assert not h.hub._running and not h.hub._inflight


def test_disable_while_sending_preserves_slot_sequence_and_drop_watermark():
    entered, release = threading.Event(), threading.Event()
    errors = []

    class Calls(list):
        def append(self, item):
            super().append(item)
            entered.set()

    def gate(plugin):
        plugin.calls = Calls()
        plugin.gate = release

    with _case(on_create=gate) as h:
        row = h.rows[0]
        h.publish(facts={'n': 1})
        _run_next(h.executor, errors)  # 分发先建槽，再把独立发送任务交给下面的线程。
        worker = threading.Thread(target=_run_next, args=(h.executor, errors))
        worker.start()
        try:
            assert entered.wait(5)
            key = str(_owner().home_dir)
            slot = h.hub._slots[key][row.manifest.plugin_id]
            h.rows = []
            h.publish(facts={'n': 2})
            h.drain()
            with h.hub._lock:
                assert h.hub._owners[key] and h.hub._latest[key]
                assert (key, row.activation.activation_id) in h.hub._inflight
                assert h.hub._slots[key][row.manifest.plugin_id] is slot
                assert slot.seq == slot.last_sent['tool_call_finished'] == 1
            h.rows = [row]
            h.publish(facts={'n': 3})
            h.publish(facts={'n': 4})
            h.drain()
        finally:
            release.set()
            worker.join(5)
        assert not worker.is_alive() and not errors
        h.drain()
        events = [event for plugin in h.plugins for _, params in plugin.calls for event in params['events']]
        assert [event['seq'] for event in events] == [1, 2]
        assert events[-1]['facts'] == {'n': 4} and events[-1]['dropped_before'] == 2
        assert h.hub._slots[key][row.manifest.plugin_id] is slot
        assert slot.last_sent['tool_call_finished'] == 4 and not h.hub._inflight


def test_each_dispatch_and_send_job_sees_its_request_and_releases_it(tmp_path):
    current = ContextVar('plugin-event-request', default=None)
    sent = threading.Event()
    seen = []

    class Request:
        def __init__(self, label):
            self.label = label

    def observe(stage):
        request = current.get()
        seen.append((stage, request.label if request is not None else None))

    executor = ThreadPoolExecutor(max_workers=1)
    with _case(executor=executor) as h:
        def installations(_owner):
            observe('dispatch')
            return tuple(h.rows)

        h.hub._installations = installations
        request = h.pool.request

        def send(connection, call):
            observe('send')
            try:
                return request(connection, call)
            finally:
                sent.set()

        h.pool.request = send

        def publish(label):
            request = Request(label)
            reference = weakref.ref(request)
            token = current.set(request)
            try:
                h.hub.publish(_owner(tmp_path / label), EventFact(type='tool_call_finished'))
            finally:
                current.reset(token)
            return reference

        for label in ('first-owner', 'later-owner'):
            sent.clear()
            reference = publish(label)
            assert sent.wait(5)
            # 单 worker 的屏障必在发送作业退出后执行；不能靠关闭整个池来消除请求引用。
            executor.submit(lambda: None).result(timeout=5)
            gc.collect()
            assert reference() is None
        assert seen == [('dispatch', 'first-owner'), ('send', 'first-owner'),
                        ('dispatch', 'later-owner'), ('send', 'later-owner')]
        assert h.hub._executor is executor
        assert all(not worker.daemon for worker in executor._threads)
