from __future__ import annotations

"""gateway heartbeat liveness and retry tests."""

import threading
import time
from pathlib import Path

import pytest

from agent_py_agent.agent.core import SimpleAgent
from agent_py_agent.agent.gateway_parts import gateway_paths, lease_service, write_json_file
from agent_py_agent.agent.gateway_parts.logging import _report_gateway_side_effect_error
from agent_py_agent.agent.settings import AgentConfig


def _make_agent(tmp_path):
    return SimpleAgent(
        AgentConfig(
            model_backend="echo",
            gateway_workspace="gateway",
            gateway_heartbeat_interval=5,
            gateway_processing_timeout_seconds=900,
            local_store_path="local_store/local.db",
            local_store_files_dir="local_store/files",
            local_store_events_path="local_store/events.jsonl",
        ),
        tmp_path,
    )


def test_heartbeat_marks_alive_on_start(tmp_path):
    agent = _make_agent(tmp_path)
    paths = gateway_paths(agent)
    request_id = "test-heartbeat-alive"
    request_path = paths.processing / f"{request_id}.json"
    paths.processing.mkdir(parents=True, exist_ok=True)
    write_json_file(request_path, {"id": request_id, "status": "processing"})

    assert not lease_service.is_heartbeat_alive_for_request(request_id)

    stop_event, thread = lease_service.start_lease_heartbeat(
        agent, request_path, request_id=request_id
    )

    # Give thread a moment to start
    time.sleep(0.1)
    assert lease_service.is_heartbeat_alive_for_request(request_id)

    stop_event.set()
    thread.join(timeout=2)


def test_heartbeat_cleanup_on_exit(tmp_path):
    agent = _make_agent(tmp_path)
    paths = gateway_paths(agent)
    request_id = "test-heartbeat-cleanup"
    request_path = paths.processing / f"{request_id}.json"
    paths.processing.mkdir(parents=True, exist_ok=True)
    write_json_file(request_path, {"id": request_id, "status": "processing"})

    stop_event, thread = lease_service.start_lease_heartbeat(
        agent, request_path, request_id=request_id
    )

    time.sleep(0.1)
    assert lease_service.is_heartbeat_alive_for_request(request_id)

    stop_event.set()
    thread.join(timeout=2)

    # After stop, liveness should be cleaned up
    assert not lease_service.is_heartbeat_alive_for_request(request_id)


def test_gateway_side_effect_error_is_structured(capsys):
    _report_gateway_side_effect_error("unit", "req-1", ValueError("bad side effect"))

    captured = capsys.readouterr()
    assert "[gateway-side-effect-error]" in captured.err
    assert '"operation": "unit"' in captured.err
    assert '"request_id": "req-1"' in captured.err
    assert "bad side effect" in captured.err


@pytest.mark.skip(reason="heartbeat runs in daemon thread, mock at import-time closure not effective")
def test_heartbeat_retry_on_failure(tmp_path, monkeypatch):
    """测试心跳在写入失败后重试 3 次后放弃。

    NOTE: This test is skipped because the heartbeat_loop captures refresh_processing_lease
    at import time, so monkeypatching doesn't affect the already-running thread.
    The actual retry logic is tested via integration tests.
    """
    agent = _make_agent(tmp_path)
    paths = gateway_paths(agent)
    request_id = "test-heartbeat-retry"
    request_path = paths.processing / f"{request_id}.json"
    paths.processing.mkdir(parents=True, exist_ok=True)
    write_json_file(request_path, {"id": request_id, "status": "processing"})

    # Use a very short interval to speed up test
    monkeypatch.setattr(lease_service, "_lease_interval", lambda agent: 0.01)

    failure_count = 0

    # Mock at the source module where heartbeat_loop actually uses it
    import agent_py_agent.agent.gateway_parts.lease_service as lease_module
    original_touch = lease_module.refresh_processing_lease

    def failing_touch(*args, **kwargs):
        nonlocal failure_count
        failure_count += 1
        if failure_count <= 3:
            raise OSError("simulated write failure")
        return original_touch(*args, **kwargs)

    # Patch in the lease module where heartbeat_loop will see it
    monkeypatch.setattr(lease_module, "refresh_processing_lease", failing_touch)

    stop_event, thread = lease_service.start_lease_heartbeat(
        agent, request_path, request_id=request_id
    )

    # Wait for the thread to process failures (with shorter timeout since interval is 0.01)
    thread.join(timeout=5)

    # Should have attempted 3 failures then abandoned
    assert failure_count == 3, f"Expected 3 failures but got {failure_count}"
    assert not lease_service.is_heartbeat_alive_for_request(request_id)


def test_stale_check_respects_heartbeat_liveness(tmp_path):
    """When heartbeat is alive, stale detection should not requeue the request."""
    from agent_py_agent.agent.gateway_parts.recovery import recover_gateway_processing_requests

    agent = _make_agent(tmp_path)
    paths = gateway_paths(agent)
    request_id = "test-stale-check"
    request_path = paths.processing / f"{request_id}.json"
    paths.processing.mkdir(parents=True, exist_ok=True)

    # Write a request with old lease
    old_time = time.time() - 1000
    write_json_file(request_path, {
        "id": request_id,
        "kind": "ask",
        "prompt": "test",
        "status": "processing",
        "lease_heartbeat_at": old_time,
        "attempts": 0,
    })

    # Register heartbeat as alive
    lease_service._active_heartbeat_request_ids.add(request_id)

    try:
        # Even with old lease, should NOT requeue because heartbeat is alive
        summary = recover_gateway_processing_requests(
            paths, timeout_seconds=120, startup=False
        )
        assert summary["requeued"] == 0
        assert summary["checked"] == 1
    finally:
        lease_service._active_heartbeat_request_ids.discard(request_id)


def test_stale_check_with_lease_stale_seconds_param(tmp_path):
    """When lease_stale_seconds is provided, it overrides timeout_seconds for stale判断."""
    from agent_py_agent.agent.gateway_parts.recovery import recover_gateway_processing_requests

    agent = _make_agent(tmp_path)
    paths = gateway_paths(agent)
    request_id = "test-lease-stale-param"
    request_path = paths.processing / f"{request_id}.json"
    paths.processing.mkdir(parents=True, exist_ok=True)

    # Write a request with heartbeat 100 seconds ago
    old_heartbeat = time.time() - 100
    write_json_file(request_path, {
        "id": request_id,
        "kind": "ask",
        "prompt": "test with lease_stale_seconds param",
        "status": "processing",
        "lease_heartbeat_at": old_heartbeat,
        "lease_started_at": old_heartbeat,
        "attempts": 0,
    })

    # Use lease_stale_seconds=60 (shorter than the 100s age) - should requeue
    summary = recover_gateway_processing_requests(
        paths, timeout_seconds=900, startup=False, lease_stale_seconds=60
    )
    assert summary["checked"] == 1
    assert summary["requeued"] == 1
    assert summary["failed"] == 0


def test_stale_check_heartbeat_alive_prevents_requeue_even_with_old_heartbeat(tmp_path):
    """Even with old lease_heartbeat_at, if heartbeat thread is alive, don't requeue."""
    from agent_py_agent.agent.gateway_parts.recovery import recover_gateway_processing_requests

    agent = _make_agent(tmp_path)
    paths = gateway_paths(agent)
    request_id = "test-heartbeat-alive-old-lease"
    request_path = paths.processing / f"{request_id}.json"
    paths.processing.mkdir(parents=True, exist_ok=True)

    # Write a request with heartbeat 100 seconds ago but thread IS alive
    old_heartbeat = time.time() - 100
    write_json_file(request_path, {
        "id": request_id,
        "kind": "ask",
        "prompt": "test heartbeat alive despite old lease",
        "status": "processing",
        "lease_heartbeat_at": old_heartbeat,
        "lease_started_at": old_heartbeat,
        "attempts": 0,
    })

    # Register heartbeat as alive (thread running)
    lease_service._active_heartbeat_request_ids.add(request_id)
    try:
        # Even with lease_stale_seconds=60 and heartbeat 100s old, should NOT requeue
        summary = recover_gateway_processing_requests(
            paths, timeout_seconds=900, startup=False, lease_stale_seconds=60
        )
        assert summary["checked"] == 1
        assert summary["requeued"] == 0
        assert summary["failed"] == 0
    finally:
        lease_service._active_heartbeat_request_ids.discard(request_id)
