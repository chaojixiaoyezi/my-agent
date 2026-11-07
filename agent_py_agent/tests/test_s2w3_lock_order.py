"""锁分类只作诊断；反例必须覆盖整个 held set，而不是栈顶。"""
import asyncio
import threading

import pytest


@pytest.fixture
def dep():
    from agent_py_agent.agent.concurrency import lock_order
    lock_order.reset_for_tests()
    lock_order.configure(strict=True)
    yield lock_order
    lock_order.reset_for_tests()


def test_held_set_detects_inversion_below_successful_try(dep):
    with dep.ordered_lock(dep.LockLevel.M, threading.RLock()):
        with dep.ordered_lock(dep.LockLevel.CG, threading.Lock(), blocking=False) as acquired:
            assert acquired
            with pytest.raises(dep.LockOrderViolation):
                with dep.ordered_lock(dep.LockLevel.T, threading.Lock()):
                    pytest.fail("held M must still prevent blocking T")
    assert dep.lockdep_snapshot()["violations"] == 1
    assert dep.held_locks() == ()


def test_reentry_and_exception_release(dep):
    lock = threading.RLock()
    with pytest.raises(ValueError):
        with dep.ordered_lock(dep.LockLevel.T, lock):
            with dep.ordered_lock(dep.LockLevel.T, lock):
                assert len(dep.held_locks()) == 2
                raise ValueError("body")
    assert dep.held_locks() == ()
    assert dep.lockdep_snapshot()["violations"] == 0


def test_distinct_same_level_is_not_reentry(dep):
    with dep.ordered_lock(dep.LockLevel.T, threading.Lock()):
        with pytest.raises(dep.LockOrderViolation):
            with dep.ordered_lock(dep.LockLevel.T, threading.Lock()):
                pytest.fail("two different T locks are not reentry")


def test_uncategorised_nested_json_is_counted_not_ordered(dep, tmp_path):
    from agent_py_agent.agent.common.json_io import locked_json_path
    with locked_json_path(tmp_path / "quota.json"):
        with locked_json_path(tmp_path / "state.json"):
            assert len(dep.held_locks()) == 2
    assert dep.lockdep_snapshot()["uncategorised"] == 2
    assert dep.lockdep_snapshot()["violations"] == 0


def test_cg_json_wrapper_is_registered_once_then_t(dep, tmp_path):
    from agent_py_agent.agent.gateway_parts.io import gateway_turn_transition
    from agent_py_agent.agent.gateway_parts.paths import gateway_paths_from_root
    from agent_py_agent.agent.subagents.coordination import subagent_creation_guard
    with subagent_creation_guard(tmp_path):
        assert [entry.level for entry in dep.held_locks()] == [dep.LockLevel.CG]
        with gateway_turn_transition(gateway_paths_from_root(tmp_path / "gateway"), "r1"):
            assert [entry.level for entry in dep.held_locks()] == [dep.LockLevel.CG, dep.LockLevel.T]
    assert dep.held_locks() == ()


def test_failed_try_records_nothing(dep):
    lock = threading.Lock()
    lock.acquire()
    try:
        with dep.ordered_lock(dep.LockLevel.CG, lock, blocking=False) as acquired:
            assert acquired is False
            assert dep.held_locks() == ()
    finally:
        lock.release()
    assert dep.lockdep_snapshot()["acquisitions"] == 0


def test_production_counts_without_changing_result(dep):
    dep.configure(strict=False)
    with dep.ordered_lock(dep.LockLevel.M, threading.Lock()):
        with dep.ordered_lock(dep.LockLevel.T, threading.Lock()):
            assert len(dep.held_locks()) == 2
    assert dep.lockdep_snapshot()["violations"] == 1


def test_strict_rejects_lock_held_across_await(dep):
    async def run():
        with dep.ordered_lock(dep.LockLevel.T, threading.Lock()):
            await asyncio.sleep(0)
    with pytest.raises(dep.LockOrderViolation):
        asyncio.run(run())
    assert dep.held_locks() == ()


def test_contract_module_is_present():
    from agent_py_agent.agent.concurrency import lock_order
    assert callable(getattr(lock_order, "ordered_lock", None))
