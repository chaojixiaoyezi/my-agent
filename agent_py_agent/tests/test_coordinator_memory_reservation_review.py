"""只控制原线程调度/异常注入，真实holder原Condition/Lock负责所有互斥。"""

import threading

import pytest

from agent_py_agent.agent.gateway_parts.coordinator_memory_resources import (
    CoordinatorMemoryResourcesUnavailable,
)
from agent_py_agent.tests.test_coordinator_memory_resources import domain, holder, home


@pytest.mark.parametrize("phase", ["future", "operation"])
@pytest.mark.parametrize("error_type", [OSError, KeyboardInterrupt])
def test_wait_error_returns_only_its_own_reservation(tmp_path, phase, error_type):
    h = holder(tmp_path)
    owner = home(tmp_path, "edge-owner")
    entered = threading.Event()
    release = threading.Event()
    errors = []

    def factory():
        if phase == "future":
            entered.set()
            assert release.wait(5)
        return domain()

    def first():
        try:
            with h.borrow_current_owner(owner, factory):
                entered.set()
                assert release.wait(5)
        except BaseException as error:
            errors.append(error)

    thread = threading.Thread(target=first)
    thread.start()
    assert entered.wait(5)
    entry = h._entries[h._key(owner)]
    original_wait = h._condition.wait
    failure = error_type("controlled original waiter failure")

    def failing_wait(*args, **kwargs):
        assert entry.borrowers == 2
        raise failure

    h._condition.wait = failing_wait
    try:
        with pytest.raises(error_type) as caught:
            with h.borrow_current_owner(
                owner, lambda: pytest.fail("same entry must not construct")
            ):
                pytest.fail("wait error must not yield")
        assert caught.value is failure
        assert entry.borrowers == 1
        assert entry.operation.locked() == (phase == "operation")
        assert h._entries[h._key(owner)] is entry
    finally:
        h._condition.wait = original_wait
        release.set()
        thread.join(5)
    assert not thread.is_alive() and not errors
    assert entry.borrowers == 0 and not entry.operation.locked()


def test_close_after_actual_acquire_releases_original_lock_and_reservation(tmp_path):
    h = holder(tmp_path)
    owner = home(tmp_path, "edge-owner")
    with h.borrow_current_owner(owner, domain):
        pass
    entry = h._entries[h._key(owner)]
    original = entry.operation

    class CloseAfterAcquire:
        def acquire(self, **kwargs):
            acquired = original.acquire(**kwargs)
            if acquired:
                h.close_admission()
            return acquired

        def release(self):
            original.release()

    entry.operation = CloseAfterAcquire()
    with pytest.raises(CoordinatorMemoryResourcesUnavailable):
        with h.borrow_current_owner(owner, lambda: pytest.fail("same entry must not construct")):
            pytest.fail("closed holder must not yield")
    assert entry.borrowers == 0 and not original.locked() and not h._entries
