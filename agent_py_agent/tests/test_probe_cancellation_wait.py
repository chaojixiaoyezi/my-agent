"""能力探针single-flight等待必须观察原取消，且不能误释放别人的探针锁。"""

import threading

from agent_py_agent.agent.backends.http import HttpBackend
from agent_py_agent.agent.concurrency.interrupt import interrupt_by_name, register_interruptible


class ObservedLock:
    def __init__(self):
        self.lock = threading.Lock()
        self.waiting = threading.Event()

    def acquire(self, blocking=True):
        if self.lock.locked():
            self.waiting.set()
        return self.lock.acquire(blocking=blocking)

    def release(self):
        self.lock.release()

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, *args):
        self.release()


def test_cancelled_probe_waiter_exits_before_other_probe_releases_lock():
    backend = object.__new__(HttpBackend)
    backend._provider_tool_capability_cache = None
    lock = backend._provider_tool_capability_lock = ObservedLock()
    lock.acquire()
    called, outcomes, done = [], [], threading.Event()
    backend._probe_tool_capability_uncached = lambda: called.append("probe")

    def waiter():
        try:
            with register_interruptible("probe-lock-cancel-test"):
                backend.probe_tool_capability()
        except InterruptedError:
            outcomes.append("interrupted")
        finally:
            done.set()

    thread = threading.Thread(target=waiter)
    thread.start()
    try:
        assert lock.waiting.wait(2)
        assert interrupt_by_name("probe-lock-cancel-test")
        assert done.wait(0.5), "cancel still waits for unrelated probe lock"
        assert lock.lock.locked(), "waiter released another execution lock"
        assert outcomes == ["interrupted"] and called == []
    finally:
        lock.release()
        thread.join(2)
    assert not thread.is_alive()


def test_probe_cancelled_at_lock_acquisition_does_not_start_or_keep_lock():
    from agent_py_agent.agent.concurrency.interrupt import set_interrupt

    class StopOnAcquire(ObservedLock):
        def acquire(self, blocking=True):
            acquired = super().acquire(blocking)
            if acquired:
                set_interrupt(True)
            return acquired

    backend = object.__new__(HttpBackend)
    backend._provider_tool_capability_cache = None
    lock = backend._provider_tool_capability_lock = StopOnAcquire()
    called = []
    backend._probe_tool_capability_uncached = lambda: called.append("probe")
    with register_interruptible("probe-acquire-cancel-test"):
        try:
            backend.probe_tool_capability()
        except InterruptedError:
            pass
        else:
            raise AssertionError("cancelled acquisition started a probe")
    assert called == [] and not lock.lock.locked()


def test_single_flight_still_reuses_positive_result_without_second_probe():
    backend = object.__new__(HttpBackend)
    backend._provider_tool_capability_cache = None
    lock = backend._provider_tool_capability_lock = ObservedLock()
    entered, release = threading.Event(), threading.Event()
    sentinel, results, errors, calls = object(), [], [], []

    def probe():
        calls.append("probe")
        entered.set()
        assert release.wait(2)
        backend._provider_tool_capability_cache = sentinel
        return sentinel

    backend._probe_tool_capability_uncached = probe

    def run():
        try:
            results.append(backend.probe_tool_capability())
        except BaseException as exc:
            errors.append(exc)

    first, second = threading.Thread(target=run), threading.Thread(target=run)
    first.start()
    try:
        assert entered.wait(2)
        second.start()
        assert lock.waiting.wait(2)
    finally:
        release.set()
        first.join(2)
        if second.ident is not None:
            second.join(2)
    assert not first.is_alive() and not second.is_alive()
    assert errors == [] and calls == ["probe"] and results == [sentinel, sentinel]


def test_probe_internal_interrupt_is_recorded_and_propagated(monkeypatch):
    from agent_py_agent.agent.backends import http

    backend = object.__new__(HttpBackend)
    backend._provider_tool_capability_cache = None
    backend._provider_tool_capability_lock = threading.Lock()
    backend.name, backend.model_name = "fake", "offline-model"
    backend.api_base, backend.stream_enabled = "https://invalid.test", False
    monkeypatch.setattr(http, "_UNACCOUNTED_PROBE_ATTEMPT_COUNT", 0)
    original = InterruptedError("cancelled fake provider")
    failed = []

    def generate(*args, **kwargs):
        raise original

    backend.generate = generate
    monkeypatch.setattr(
        http, "_fail_probe_call", lambda scope, call_id, error: failed.append(error)
    )
    try:
        backend.probe_tool_capability()
    except InterruptedError as error:
        assert error is original
    else:
        raise AssertionError("interrupt became an unsupported capability")
    assert failed == [original]
    assert backend._provider_tool_capability_cache is None
    assert not backend._provider_tool_capability_lock.locked()


def test_cancelled_vision_waiter_exits_before_other_probe_releases_lock(monkeypatch):
    from types import SimpleNamespace

    from agent_py_agent.agent.backends import vision_capability as vision

    backend = SimpleNamespace(api_base="https://vision-wait.invalid", model_name="fake")
    key = vision.vision_capability_cache_key(backend)
    lock = ObservedLock()
    monkeypatch.setitem(vision._KEY_LOCKS, key, lock)
    lock.acquire()
    called, outcomes, done = [], [], threading.Event()

    def unexpected_probe(*args):
        called.append("probe")
        return vision.VisionCapability(vision.VISION_INCONCLUSIVE, "probe_inconclusive")

    monkeypatch.setattr(vision, "probe_vision_capability", unexpected_probe)

    def waiter():
        try:
            with register_interruptible("vision-lock-cancel-test"):
                vision.resolve_vision_capability(backend)
        except InterruptedError:
            outcomes.append("interrupted")
        finally:
            done.set()

    thread = threading.Thread(target=waiter)
    thread.start()
    try:
        assert lock.waiting.wait(2)
        assert interrupt_by_name("vision-lock-cancel-test")
        assert done.wait(0.5), "vision cancel still waits for unrelated probe lock"
        assert lock.lock.locked()
        assert outcomes == ["interrupted"] and called == []
    finally:
        lock.release()
        thread.join(2)
    assert not thread.is_alive()


def test_vision_probe_interrupt_is_not_capability_unavailable(monkeypatch):
    from types import SimpleNamespace

    from agent_py_agent.agent.backends import vision_capability as vision

    original = InterruptedError("cancelled fake vision provider")

    def generate(*args, **kwargs):
        raise original

    backend = SimpleNamespace(
        api_base="https://vision-interrupt.invalid",
        model_name="fake",
        generate=generate,
        probe_tool_capability=lambda: SimpleNamespace(native_supported=True),
    )
    key = vision.vision_capability_cache_key(backend)
    monkeypatch.setitem(vision._KEY_LOCKS, key, threading.Lock())
    try:
        vision.resolve_vision_capability(backend)
    except InterruptedError as error:
        assert error is original
    else:
        raise AssertionError("vision interrupt became a capability fact")
    assert key not in vision._CACHE
    assert not vision._KEY_LOCKS[key].locked()


def test_vision_single_flight_keeps_positive_cache(monkeypatch):
    from types import SimpleNamespace

    from agent_py_agent.agent.backends import vision_capability as vision

    backend = SimpleNamespace(api_base="https://vision-single-flight.invalid", model_name="fake")
    key = vision.vision_capability_cache_key(backend)
    lock = ObservedLock()
    monkeypatch.setitem(vision._KEY_LOCKS, key, lock)
    monkeypatch.setitem(vision._CACHE, key, None)
    entered, release = threading.Event(), threading.Event()
    sentinel = vision.VisionCapability(vision.VISION_SUPPORTED, "probe_supported")
    results, errors, calls = [], [], []

    def probe(*args):
        calls.append("probe")
        entered.set()
        assert release.wait(2)
        return sentinel

    monkeypatch.setattr(vision, "probe_vision_capability", probe)

    def run():
        try:
            results.append(vision.resolve_vision_capability(backend))
        except BaseException as error:
            errors.append(error)

    first, second = threading.Thread(target=run), threading.Thread(target=run)
    first.start()
    try:
        assert entered.wait(2)
        second.start()
        assert lock.waiting.wait(2)
    finally:
        release.set()
        first.join(2)
        if second.ident is not None:
            second.join(2)
    assert not first.is_alive() and not second.is_alive()
    assert results == [sentinel, sentinel] and calls == ["probe"] and errors == []
    assert vision.resolve_vision_capability(backend) is sentinel
