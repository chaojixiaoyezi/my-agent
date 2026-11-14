from __future__ import annotations

import tempfile
import time
from pathlib import Path

from agent_py_agent.agent.agent_core.models import AgentRunResult
from agent_py_agent.agent.backends import get_backend
from agent_py_agent.agent.core import SimpleAgent
from agent_py_agent.agent.gateway_parts import (
    GatewayAskParams,
    _handle_gateway_request,
    _process_gateway_requests,
    gateway_paths,
    read_json_file,
    recover_gateway_processing_requests,
    submit_gateway_ask,
    write_json_file,
)
from agent_py_agent.agent.gateway_parts.recovery import (
    MAX_UNPLANNED_RESUME_COUNT,
    repair_gateway_terminal_projections,
)
from agent_py_agent.agent.gateway_parts.request_worker import _iter_pending_request_paths
from agent_py_agent.agent.io import append_jsonl
from agent_py_agent.agent.local_storage import LocalStore
from agent_py_agent.agent.memory_store import JsonlMemory
from agent_py_agent.agent.settings import AgentConfig
from agent_py_agent.cli.local_doctor import build_local_doctor_report, rebuild_local_store


def _wait_for_lease_heartbeat(processing_path, *, greater_than=0.0) -> float:
    deadline = time.time() + 2
    value = 0.0
    while time.time() < deadline:
        time.sleep(0.02)
        value = float(read_json_file(processing_path).get("lease_heartbeat_at", 0) or 0)
        if value > greater_than:
            break
    return value


def _track_heartbeat_during_run(agent, paths, request_path) -> list[float]:
    observed: list[float] = []
    original_run = agent.run
    processing_path = paths.processing / request_path.name

    def slow_run(user_prompt: str, **kwargs) -> AgentRunResult:
        initial = _wait_for_lease_heartbeat(processing_path)
        updated = _wait_for_lease_heartbeat(processing_path, greater_than=initial)
        observed.extend([initial, updated])
        return AgentRunResult(
            prompt=f"prompt: {user_prompt}",
            response="ok",
            backend="test",
            used_memories=0,
        )

    agent.run = slow_run  # type: ignore[method-assign]
    try:
        assert _process_gateway_requests(agent, paths, worker_id="test-worker") == 1
    finally:
        agent.run = original_run  # type: ignore[method-assign]
    return observed


def _make_recovery_agent(root: Path) -> SimpleAgent:
    cfg = AgentConfig(
        model_backend="echo",
        gateway_workspace="gateway",
        local_store_path="local_store/local.db",
        local_store_files_dir="local_store/files",
        local_store_events_path="local_store/events.jsonl",
        gateway_processing_timeout_seconds=1,
        gateway_request_max_attempts=2,
    )
    agent = SimpleAgent(cfg, root)
    # 有 Home 时目录未选模型即未配置；离线 echo 后端须构造后显式注入，才在 Gateway 模型作用域里保留。
    agent.backend = get_backend("echo", cfg)
    return agent


def _ensure_gateway_dirs(paths) -> None:
    for path in (paths.inbox, paths.processing, paths.done, paths.failed, paths.responses):
        path.mkdir(parents=True, exist_ok=True)


def _write_processing_request(paths, request_id: str, attempts: int, prompt: str) -> Path:
    request_path = paths.processing / f"{request_id}.json"
    write_json_file(
        request_path,
        {
            "id": request_id,
            "kind": "ask",
            "prompt": prompt,
            "attempts": attempts,
            "status": "processing",
            "turn_phase": "open",
            "lease_started_at": time.time() - 10,
        },
    )
    return request_path


def test_gateway_processing_recovery_requeues_then_fails_after_attempt_limit():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        agent = _make_recovery_agent(root)
        paths = gateway_paths(agent)
        _ensure_gateway_dirs(paths)

        request_path = _write_processing_request(paths, "gwreq-timeout", 1, "会被重排的请求")
        recovered = recover_gateway_processing_requests(
            paths,
            startup=False,
            max_attempts=2,
            timeout_seconds=1,
            agent=agent,
        )
        assert recovered["requeued"] == 1
        assert (paths.inbox / request_path.name).exists()

        second_path = _write_processing_request(paths, "gwreq-fail", 2, "会失败归档的请求")
        payload = read_json_file(second_path)
        payload["processing_failure_count"] = 1
        write_json_file(second_path, payload)
        failed = recover_gateway_processing_requests(
            paths,
            startup=False,
            max_attempts=2,
            timeout_seconds=1,
            agent=agent,
        )
        assert failed["failed"] == 1
        assert (paths.failed / second_path.name).exists()
        assert (paths.responses / "gwreq-fail.json").exists()


def test_repeated_gateway_restart_is_not_a_processing_failure(tmp_path):
    agent = _make_recovery_agent(tmp_path)
    paths = gateway_paths(agent)
    _ensure_gateway_dirs(paths)
    path = _write_processing_request(paths, "gwreq-restarts", 20, "继续原长任务")
    # 重启不算卡死次数；非计划重启的续跑另有上限（用完收成 TURN_RESUME_LIMIT_EXCEEDED，见 test_turn_resume_limit）。
    for attempt in range(20, 20 + MAX_UNPLANNED_RESUME_COUNT):
        result = recover_gateway_processing_requests(paths, startup=True, agent=agent)
        assert result["requeued"] == 1
        assert result["failed"] == 0
        pending = paths.inbox / path.name
        payload = read_json_file(pending)
        assert payload["attempts"] == attempt
        assert payload["processing_failure_count"] == 0
        assert payload["active_turn_recovery"]["cause"] == "gateway_restart"
        assert payload["active_turn_recovery"]["unplanned_resume_count"] == attempt - 19
        assert payload["id"] == "gwreq-restarts"
        assert not (paths.terminal / path.name).exists()
        payload.update(status="processing", attempts=attempt + 1, lease_started_at=time.time() - 20)
        write_json_file(pending, payload)
        pending.replace(path)

    first = recover_gateway_processing_requests(paths, startup=False, timeout_seconds=1, agent=agent)
    assert first["requeued"] == 1
    pending = paths.inbox / path.name
    payload = read_json_file(pending)
    assert payload["processing_failure_count"] == 1
    assert payload["active_turn_recovery"]["cause"] == "processing_lease_expired"
    payload.update(status="processing", attempts=25, lease_started_at=time.time() - 20)
    write_json_file(pending, payload)
    pending.replace(path)
    second = recover_gateway_processing_requests(paths, startup=False, timeout_seconds=1, agent=agent)
    assert second["failed"] == 1
    response = read_json_file(paths.responses / path.name)
    assert response["processing_failure_count"] == 2
    assert response["attempts"] == 25


def test_restart_does_not_erase_a_prior_processing_failure(tmp_path):
    agent = _make_recovery_agent(tmp_path)
    paths = gateway_paths(agent)
    _ensure_gateway_dirs(paths)
    path = _write_processing_request(paths, "gwreq-prior-failure", 2, "继续工作")
    payload = read_json_file(path)
    payload["processing_failure_count"] = 1
    write_json_file(path, payload)
    result = recover_gateway_processing_requests(paths, startup=True, agent=agent)
    assert result["requeued"] == 1
    assert read_json_file(paths.inbox / path.name)["processing_failure_count"] == 1


def test_corrupt_processing_failure_counter_is_not_silently_reset(tmp_path):
    from agent_py_agent.agent.gateway_parts.recovery import (
        recover_gateway_processing_requests_report,
    )

    agent = _make_recovery_agent(tmp_path)
    paths = gateway_paths(agent)
    _ensure_gateway_dirs(paths)
    path = _write_processing_request(paths, "gwreq-bad-counter", 2, "继续工作")
    payload = read_json_file(path)
    payload["processing_failure_count"] = "unknown"
    write_json_file(path, payload)
    report = recover_gateway_processing_requests_report(paths, startup=True, agent=agent)
    assert report.summary["requeued"] == report.summary["failed"] == 0
    assert len(report.load_errors) == 1
    assert read_json_file(path)["processing_failure_count"] == "unknown"


def test_gateway_startup_requeued_request_does_not_block_fresh_pending():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        agent = _make_recovery_agent(root)
        paths = gateway_paths(agent)
        _ensure_gateway_dirs(paths)

        old_path = _write_processing_request(paths, "gwreq-old", 0, "旧请求")
        recovered = recover_gateway_processing_requests(
            paths,
            startup=True,
            max_attempts=2,
            timeout_seconds=1,
            agent=agent,
        )
        assert recovered["requeued"] == 1
        old_pending = paths.inbox / old_path.name
        old_payload = read_json_file(old_pending)
        assert old_payload["not_before_at"] > time.time()
        assert old_payload["priority"] == "recovery"
        # provider_resume_count 是供应商故障自动续跑的独立计数（tresume）：重启重排原样带过，新请求从 0。
        assert old_payload["active_turn_recovery"] == {
            "schema_version": "gateway_active_turn_recovery.v1",
            "request_id": "gwreq-old",
            "dead_execution_attempt_id": "",
            "requeued_at": old_payload["requeued_at"],
            "cause": "gateway_restart",
            "unplanned_resume_count": 1,
            "provider_resume_count": 0,
        }

        fresh_id, fresh_path, _ = submit_gateway_ask(
            paths, params=GatewayAskParams(prompt="新请求", save=False)
        )
        assert _iter_pending_request_paths(paths)[0].name == fresh_path.name
        assert _process_gateway_requests(agent, paths, worker_id="test-worker") == 1

        assert (paths.responses / f"{fresh_id}.json").exists()
        assert (paths.done / fresh_path.name).exists()
        assert old_pending.exists()
        assert not (paths.responses / "gwreq-old.json").exists()


def test_gateway_terminal_projector_skips_completed_projection(monkeypatch, tmp_path):
    agent = _make_recovery_agent(tmp_path)
    paths = gateway_paths(agent)
    _ensure_gateway_dirs(paths)
    request_id, _request_path, _created = submit_gateway_ask(
        paths,
        params=GatewayAskParams(prompt="终态投影只修复一次", save=False),
    )
    assert _process_gateway_requests(agent, paths, worker_id="marker-worker") == 1
    marker_dir = paths.root / "projection_state" / "terminal_complete"
    marker = marker_dir / f"{request_id}.json"
    assert marker.exists()

    def refuse_repeat(*_args, **_kwargs):
        raise AssertionError("completed terminal projection must not replay")

    monkeypatch.setattr(
        "agent_py_agent.agent.gateway_parts.recovery._recover_committed_terminal_processing",
        refuse_repeat,
    )
    projected, errors = repair_gateway_terminal_projections(paths, agent=agent, limit=16)

    assert projected == 0
    assert errors == []


def test_gateway_terminal_marker_invalidates_when_response_projection_changes(tmp_path):
    agent = _make_recovery_agent(tmp_path)
    paths = gateway_paths(agent)
    _ensure_gateway_dirs(paths)
    request_id, _request_path, _created = submit_gateway_ask(
        paths,
        params=GatewayAskParams(prompt="缺失投影必须自动补回", save=False),
    )
    assert _process_gateway_requests(agent, paths, worker_id="repair-worker") == 1
    response_path = paths.responses / f"{request_id}.json"
    response_path.unlink()

    projected, errors = repair_gateway_terminal_projections(paths, agent=agent, limit=16)

    assert projected == 1
    assert errors == []
    assert response_path.exists()


def test_gateway_worker_prioritizes_fresh_pending_over_due_recovery_request():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        agent = _make_recovery_agent(root)
        paths = gateway_paths(agent)
        _ensure_gateway_dirs(paths)

        recovery_path = paths.inbox / "gwreq-recovery.json"
        write_json_file(
            recovery_path,
            {
                "id": "gwreq-recovery",
                "kind": "ask",
                "prompt": "旧恢复请求",
                "status": "pending",
                "priority": "recovery",
                "requeued_at": time.time() - 60,
                "not_before_at": time.time() - 1,
            },
        )
        _fresh_id, fresh_path, _ = submit_gateway_ask(
            paths, params=GatewayAskParams(prompt="新请求", save=False)
        )

        ordered = _iter_pending_request_paths(paths)

        assert ordered[0].name == fresh_path.name
        assert ordered[-1].name == recovery_path.name


def test_gateway_worker_refreshes_processing_lease_heartbeat_during_long_run():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        cfg = AgentConfig(
            model_backend="echo",
            gateway_workspace="gateway",
            gateway_processing_timeout_seconds=1,
            local_store_path="local_store/local.db",
            local_store_files_dir="local_store/files",
            local_store_events_path="local_store/events.jsonl",
        )
        agent = SimpleAgent(cfg, root)
        paths = gateway_paths(agent)
        for path in (paths.inbox, paths.processing, paths.done, paths.failed, paths.responses):
            path.mkdir(parents=True, exist_ok=True)
        request_id, request_path, _ = submit_gateway_ask(
            paths, params=GatewayAskParams(prompt="长任务 lease heartbeat 测试", save=False)
        )
        observed = _track_heartbeat_during_run(agent, paths, request_path)

        archived = read_json_file(paths.done / request_path.name)
        response = read_json_file(paths.responses / f"{request_id}.json")
        assert observed[0] > 0
        assert observed[1] > observed[0]
        assert archived["status"] == "done"
        assert str(archived["lease_owner"]).startswith("gateway-attempt-")
        assert archived["lease_owner"] == archived["execution_attempt_id"]
        assert archived["lease_heartbeat_at"] >= observed[1]
        assert response["ok"] is True
        assert response["lease_heartbeat_at"] >= observed[1]
