from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from agent_py_agent.agent.common.json_io import locked_json_path
from agent_py_agent.agent.memory_store.curator_lease import release_curator_lease
from agent_py_agent.agent.memory_store.curator_state import CuratorLeaseRequest
from agent_py_agent.tests.test_conveyor_m64_curator_catchup_and_consumption import _append_input
from agent_py_agent.tests.test_curator_observation_identity import _service
from agent_py_agent.tests.test_d2_curator_breaker_shrink_quarantine import _ShapeBackend, _thread


@pytest.mark.parametrize("reason", ["task_complete", "pre_compact", "session_close", "interval"])
def test_first_scheduled_extraction_freezes_cutoff_after_candidate_maintenance(tmp_path, reason):
    store, thread, _ = _thread(tmp_path, 0)
    now = datetime(2026, 11, 20, 12, tzinfo=timezone.utc)
    _append_input(store, thread, now - timedelta(hours=4), 1)
    recent = _append_input(store, thread, now - timedelta(minutes=5), 2)
    backend = _ShapeBackend(thread.thread_id, lambda _: False)
    service = _service(tmp_path, backend, store)
    service.config = replace(service.config, interval_seconds=3600)
    with locked_json_path(service.state_store.path):
        _, lease = service.state_store._acquire_unlocked(CuratorLeaseRequest(
            "interval", service.config.revision(), 60, now, purpose="candidate_scan",
        ))
    release_curator_lease(service.state_store, lease["lease_id"])

    result = service.run(reason=reason, scheduled=True, now=now)

    assert result.processed_messages == 1
    assert backend.seen == [[recent.message_id]]
    assert service.state_store.load().first_enable_after == (now - timedelta(hours=1)).timestamp()


def test_detached_replay_preserves_frozen_first_enable_cutoff(tmp_path):
    from agent_py_agent.agent.memory_store.curator_models import (
        CURATOR_REPLAY_BREAKER_FAILURE_COUNT,
    )

    store, thread, _ = _thread(tmp_path, 0)
    now = datetime(2026, 11, 20, 12, tzinfo=timezone.utc)
    _append_input(store, thread, now - timedelta(hours=4), 1)
    recent = _append_input(store, thread, now - timedelta(minutes=5), 2)
    backend = _ShapeBackend(thread.thread_id, lambda _: True)
    service = _service(tmp_path, backend, store)
    service.config = replace(service.config, interval_seconds=3600)

    for _ in range(CURATOR_REPLAY_BREAKER_FAILURE_COUNT):
        assert service.run(reason="task_complete", now=now, scheduled=True).failure_code == "CURATOR_SCHEMA_INVALID"
    detached, = service.state_store.load().pending_local_dates
    assert detached["source_range"]["first_enable_after"] == (now - timedelta(hours=1)).timestamp()
    backend.fails = lambda _: False

    result = service.run(reason="interval", now=now, scheduled=True, date_replay=detached)

    assert (result.status, result.processed_messages) == ("succeeded", 1)
    assert backend.seen == [[recent.message_id]] * (CURATOR_REPLAY_BREAKER_FAILURE_COUNT + 1)
    assert service.state_store.load().pending_local_dates == []
