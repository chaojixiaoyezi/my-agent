"""W1: a busy lease beat keeps the heartbeat alive; a superseded attempt's late archive is a stale claim."""
from __future__ import annotations

import threading
from pathlib import Path
from unittest.mock import MagicMock

from agent_py_agent.agent.gateway_parts import lease_service
from agent_py_agent.agent.gateway_parts.io import (
    gateway_turn_transition,
    read_json_file,
    write_json_file,
)
from agent_py_agent.agent.gateway_parts.paths import gateway_paths_from_root
from agent_py_agent.agent.gateway_parts.queue_service import (
    GatewayArchiveDisposition,
    archive_request,
)


def _context(request_path: Path, request_id: str, attempt: str = "att-1") -> lease_service._HeartbeatLoopContext:
    return lease_service._HeartbeatLoopContext(
        MagicMock(), request_path, request_id, "w-1", attempt, 1, threading.Event(), 0.01,
    )


def _processing_request(tmp_path: Path, request_id: str, attempt: str = "att-1") -> Path:
    path = tmp_path / "requests" / "processing" / f"{request_id}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    write_json_file(path, {
        "id": request_id, "status": "processing", "lease_owner": "w-1",
        "execution_attempt_id": attempt, "lease_epoch": 1, "lease_heartbeat_at": 1.0,
    })
    return path


def test_busy_beats_keep_heartbeat_alive_and_refresh_after_release(tmp_path, monkeypatch):
    request_path = _processing_request(tmp_path, "req-busy")
    real = lease_service.refresh_processing_lease_report
    calls = {"n": 0}

    def flaky(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] <= 4:
            return lease_service.LeaseRefreshReport(False, busy=True)
        return real(*args, **kwargs)

    monkeypatch.setattr(lease_service, "refresh_processing_lease_report", flaky)
    context = _context(request_path, "req-busy")
    thread = threading.Thread(target=lease_service._run_heartbeat_loop_body, args=(context,), daemon=True)
    thread.start()
    try:
        deadline = threading.Event()
        for _ in range(200):
            if calls["n"] >= 6 and read_json_file(request_path)["lease_heartbeat_at"] > 1.0:
                break
            deadline.wait(0.01)
        assert thread.is_alive()
        assert read_json_file(request_path)["lease_heartbeat_at"] > 1.0
    finally:
        context.stop_event.set()
        thread.join(timeout=2)


def test_real_turn_lock_contention_reports_busy(tmp_path):
    request_path = _processing_request(tmp_path, "req-lock")
    paths = gateway_paths_from_root(tmp_path)
    with gateway_turn_transition(paths, "req-lock"):
        report = lease_service.refresh_processing_lease_report(
            request_path, request_id="req-lock", worker_id="w-1", execution_attempt_id="att-1", lease_epoch=1,
        )
        counted = lease_service._refresh_or_count_failure(request_path, "req-lock", "w-1", "att-1", 1, 2)
    assert (report.ok, report.busy, report.load_error) == (False, True, None)
    assert counted == (True, 2)
    assert read_json_file(request_path)["lease_heartbeat_at"] == 1.0


def test_attempt_mismatch_still_stops_on_next_beat(tmp_path):
    request_path = _processing_request(tmp_path, "req-moved", attempt="att-2")
    context = _context(request_path, "req-moved", attempt="att-1")
    thread = threading.Thread(target=lease_service._run_heartbeat_loop_body, args=(context,), daemon=True)
    thread.start()
    thread.join(timeout=2)
    assert not thread.is_alive()
    assert read_json_file(request_path)["lease_heartbeat_at"] == 1.0


def test_load_errors_stop_after_three_with_one_audit(tmp_path, monkeypatch):
    request_path = _processing_request(tmp_path, "req-bad")
    audits = []
    monkeypatch.setattr(lease_service, "audit_heartbeat_abandoned", lambda *a: audits.append(a))
    monkeypatch.setattr(
        lease_service, "refresh_processing_lease_report",
        lambda *a, **k: lease_service.LeaseRefreshReport(False, {"context": "x", "path": "p", "message": "m"}),
    )
    context = _context(request_path, "req-bad")
    thread = threading.Thread(target=lease_service._run_heartbeat_loop_body, args=(context,), daemon=True)
    thread.start()
    thread.join(timeout=2)
    assert not thread.is_alive()
    assert len(audits) == 1 and audits[0][2] == 3


def _canonical_terminal(tmp_path: Path, request_id: str, attempt: str) -> Path:
    paths = gateway_paths_from_root(tmp_path)
    paths.terminal.mkdir(parents=True, exist_ok=True)
    write_json_file(paths.terminal / f"{request_id}.json", {
        "schema_version": "gateway_terminal_request.v1", "id": request_id, "status": "failed",
        "execution_attempt_id": attempt,
        "terminal_response": {"id": request_id, "ok": False, "status": "failed", "error_code": "GATEWAY_PROCESSING_TIMEOUT"},
    })
    return paths.processing / f"{request_id}.json"


def test_superseded_attempt_late_archive_is_stale_claim(tmp_path, monkeypatch):
    reports = []
    from agent_py_agent.agent.gateway_parts import logging as gateway_logging
    monkeypatch.setattr(gateway_logging, "_report_gateway_side_effect_error", lambda *a, **k: reports.append(a))
    processing = _canonical_terminal(tmp_path, "req-late", attempt="att-2")
    paths = gateway_paths_from_root(tmp_path)
    result = archive_request(
        paths, processing, paths.done, "req-late",
        terminal_response={"id": "req-late", "ok": True, "status": "done", "response": "late"},
        expected_execution_attempt_id="att-1", expected_lease_epoch=1,
    )
    assert result.disposition is GatewayArchiveDisposition.STALE_CLAIM
    assert not [r for r in reports if r and r[0] == "archive_gateway_request_conflict"]


def test_same_attempt_different_outcome_is_still_conflict(tmp_path):
    processing = _canonical_terminal(tmp_path, "req-same", attempt="att-1")
    paths = gateway_paths_from_root(tmp_path)
    result = archive_request(
        paths, processing, paths.done, "req-same",
        terminal_response={"id": "req-same", "ok": True, "status": "done", "response": "other"},
        expected_execution_attempt_id="att-1", expected_lease_epoch=1,
    )
    assert result.disposition is GatewayArchiveDisposition.CONFLICT
