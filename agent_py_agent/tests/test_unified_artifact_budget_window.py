"""精确600秒窗口、并发在途额度和有界闲置回收，不用maxlen提前释放收费。"""
import asyncio
from collections import deque
from concurrent.futures import ThreadPoolExecutor

import pytest

from agent_py_agent.agent.tooling import artifact
from agent_py_agent.agent.tooling.artifact import ArtifactReadBudget, ArtifactReadBudgetRequest


@pytest.fixture
def clock(monkeypatch):
    now = [0.0]
    monkeypatch.setattr(artifact.time, "monotonic", lambda: now[0])
    return now


def reserve(budget, amount, run="run"):
    return budget.reserve(ArtifactReadBudgetRequest(run, amount))


def charge(budget, amount, run="run"):
    token, error = reserve(budget, amount, run)
    assert token is not None and not error
    budget.settle(token, amount)


def test_exact_expiry_boundary_and_actual_chars(clock):
    budget = ArtifactReadBudget(window_seconds=600, max_chars=10)
    clock[0] = 10
    token, error = reserve(budget, 10)
    assert not error
    budget.settle(token, 3)
    clock[0] = 609.999
    assert reserve(budget, 8)[1]
    token, error = reserve(budget, 7)
    assert token is not None and not error
    budget.settle(token, 0)
    clock[0] = 610
    token, error = reserve(budget, 10)
    assert token is not None and not error
    budget.settle(token, 0)
    assert budget._events_by_run == budget._pending == {}


def test_late_settlement_charges_from_real_completion_not_reservation(clock):
    budget = ArtifactReadBudget(window_seconds=600, max_chars=6)
    token, error = reserve(budget, 6)
    assert not error
    clock[0] = 601
    assert reserve(budget, 1)[1], "在途预留不能随收费窗口自动失效"
    budget.settle(token, 6)
    assert reserve(budget, 1)[1]
    clock[0] = 1201
    token, error = reserve(budget, 6)
    assert token is not None and not error
    budget.settle(token, 0)


def test_pending_is_atomic_per_run_and_other_runs_are_independent(clock):
    budget = ArtifactReadBudget(window_seconds=600, max_chars=100)
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _: reserve(budget, 25), range(16)))
    accepted = [token for token, error in results if not error]
    assert len(accepted) == 4
    assert len(budget._pending) == 4
    other, error = reserve(budget, 100, "other")
    assert other is not None and not error
    for token in accepted:
        budget.settle(token, 25)
    budget.settle(other, 0)
    assert reserve(budget, 1)[1]
    assert not budget._pending


def test_failure_duplicate_and_unknown_settlement_never_double_charge(clock):
    budget = ArtifactReadBudget(window_seconds=600, max_chars=10)
    token, _ = reserve(budget, 10)
    budget.settle(token, 0)
    assert budget._pending == budget._events_by_run == {}
    token, _ = reserve(budget, 10)
    budget.settle(token, 3)
    budget.settle(token, 100)
    budget.settle(object(), 100)
    next_token, error = reserve(budget, 7)
    assert next_token is not None and not error
    budget.settle(next_token, 0)
    assert reserve(budget, 8)[1]


def test_capacity_does_not_drop_unexpired_events(clock):
    budget = ArtifactReadBudget(window_seconds=600, max_chars=1500)
    for index in range(1500):
        clock[0] = index / 10000
        charge(budget, 1)
    assert reserve(budget, 1)[1]
    clock[0] = 599.999
    assert reserve(budget, 1)[1]
    clock[0] = 600
    token, error = reserve(budget, 1)
    assert token is not None and not error
    assert reserve(budget, 1)[1], "只有首个事件恰好过期，不能成桶近似或一次放出全部额度"
    budget.settle(token, 0)


def test_live_history_is_deque_and_never_resummed(clock):
    budget = ArtifactReadBudget(window_seconds=600, max_chars=1000)
    charge(budget, 10)
    assert isinstance(budget._events_by_run["run"], deque)

    class NoIteration(deque):
        def __iter__(self):
            raise AssertionError("live_event_history_was_scanned")

    budget._events_by_run["run"] = NoIteration(budget._events_by_run["run"])
    token, error = reserve(budget, 10)
    assert token is not None and not error
    budget.settle(token, 5)
    assert budget._used_by_run["run"] == 15


def test_reserve_does_not_scan_pending_of_other_runs(clock):
    budget = ArtifactReadBudget(window_seconds=600, max_chars=10)
    token, _ = reserve(budget, 10, "other")

    class NoValues(dict):
        def values(self):
            raise AssertionError("all_pending_runs_were_scanned")

    budget._pending = NoValues(budget._pending)
    own, error = reserve(budget, 10)
    assert own is not None and not error
    budget.settle(own, 0)
    budget.settle(token, 0)
    assert not budget._pending


def test_idle_runs_are_reclaimed_incrementally_on_next_operations(clock):
    budget = ArtifactReadBudget(window_seconds=600, max_chars=10)
    for index in range(200):
        charge(budget, 1, str(index))
    assert len(budget._events_by_run) == 200
    clock[0] = 601
    budget.settle(None, 0)
    assert 0 < len(budget._events_by_run) < 200, "每次有界推进，既不扫全表也不永留闲置run"
    for _ in range(200):
        budget.settle(None, 0)
    assert not budget._events_by_run and not budget._used_by_run
    assert not budget._pending_by_run and not budget._maintenance_runs


@pytest.mark.parametrize("window,maximum,run,amount", [(0, 10, "run", 10), (600, 0, "run", 10),
                                                       (600, 10, "", 10), (600, 10, "run", 0)])
def test_zero_budget_and_unscoped_paths_keep_no_state(clock, window, maximum, run, amount):
    budget = ArtifactReadBudget(window_seconds=window, max_chars=maximum)
    assert reserve(budget, amount, run) == (None, "")
    budget.settle(None, 0)
    assert budget._events_by_run == budget._pending == {}


@pytest.mark.parametrize("failure", [RuntimeError, asyncio.CancelledError, KeyboardInterrupt])
def test_real_tool_finally_releases_on_errors_and_cancellation(tmp_path, monkeypatch, failure):
    tool = artifact.ReadArtifactTool(tmp_path, artifact_read_budget_max_chars=10)

    def fail_read(*args, **kwargs):
        raise failure()

    monkeypatch.setattr(artifact, "read_tool_output_artifact", fail_read)
    with pytest.raises(failure):
        tool.execute({"artifact_ref": "fixture", "run_id": "run", "max_chars": 10})
    assert not tool.read_budget._pending and not tool.read_budget._pending_by_run
    assert not tool.read_budget._events_by_run and not tool.read_budget._used_by_run


def test_idle_maintenance_limits_expired_events_but_target_run_expires_exactly(clock):
    budget = ArtifactReadBudget(window_seconds=600, max_chars=100)
    for index in range(100):
        clock[0] = index / 1000
        charge(budget, 1, "cold")
    clock[0] = 700
    budget.settle(None, 0)
    assert len(budget._events_by_run["cold"]) == budget._used_by_run["cold"] == 92
    token, error = reserve(budget, 100, "cold")
    assert token is not None and not error
    assert "cold" not in budget._events_by_run and "cold" not in budget._used_by_run
    budget.settle(token, 0)
