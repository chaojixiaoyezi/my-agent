from __future__ import annotations

import json
import tempfile
import threading
import time
from pathlib import Path

from agent_py_agent.agent.agent_core.models import AgentRunResult
from agent_py_agent.agent.core import SimpleAgent
from agent_py_agent.agent.gateway_parts import (
    AdapterPaths,
    GatewayAskParams,
    _handle_gateway_request,
    _process_gateway_requests,
    gateway_paths,
    gateway_stale_processing,
    process_file_adapter_once,
    read_json_file,
    recover_gateway_processing_requests,
    submit_gateway_ask,
    write_json_file,
)
from agent_py_agent.agent.gateway_parts.request_worker import _iter_pending_request_paths
from agent_py_agent.agent.io import append_jsonl
from agent_py_agent.agent.local_storage import LocalStore
from agent_py_agent.agent.memory_store import JsonlMemory
from agent_py_agent.agent.settings import AgentConfig
from agent_py_agent.cli.local_doctor import build_local_doctor_report, rebuild_local_store


def _process_gateway_once_when_inbox_ready(agent, gpaths) -> None:
    deadline = time.time() + 5
    while time.time() < deadline:
        if list(gpaths.inbox.glob("*.json")):
            _process_gateway_requests(agent, gpaths)
            return
        time.sleep(0.05)


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
    return SimpleAgent(cfg, root)


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

        fresh_id, fresh_path, _ = submit_gateway_ask(
            paths, params=GatewayAskParams(prompt="新请求", save=False)
        )
        assert _iter_pending_request_paths(paths)[0].name == fresh_path.name
        assert _process_gateway_requests(agent, paths, worker_id="test-worker") == 1

        assert (paths.responses / f"{fresh_id}.json").exists()
        assert (paths.done / fresh_path.name).exists()
        assert old_pending.exists()
        assert not (paths.responses / "gwreq-old.json").exists()


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
            gateway_heartbeat_interval=1,
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


def test_gateway_recovery_uses_lease_heartbeat_before_started_at():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        cfg = AgentConfig(
            model_backend="echo",
            gateway_workspace="gateway",
            local_store_path="local_store/local.db",
            local_store_files_dir="local_store/files",
            local_store_events_path="local_store/events.jsonl",
        )
        agent = SimpleAgent(cfg, root)
        paths = gateway_paths(agent)
        for path in (paths.inbox, paths.processing, paths.done, paths.failed, paths.responses):
            path.mkdir(parents=True, exist_ok=True)

        request_path = paths.processing / "gwreq-heartbeat-fresh.json"
        write_json_file(
            request_path,
            {
                "id": "gwreq-heartbeat-fresh",
                "kind": "ask",
                "prompt": "heartbeat 还新，不能按 started_at 误判 stale",
                "status": "processing",
                "attempts": 1,
                "lease_owner": "test-worker",
                "lease_started_at": time.time() - 10,
                "lease_heartbeat_at": time.time(),
            },
        )

        assert gateway_stale_processing(paths, timeout_seconds=1) == []
        recovered = recover_gateway_processing_requests(
            paths,
            startup=False,
            max_attempts=2,
            timeout_seconds=1,
            agent=agent,
        )
        assert recovered["checked"] == 1
        assert recovered["requeued"] == 0
        assert recovered["failed"] == 0
        assert request_path.exists()
        assert not (paths.inbox / request_path.name).exists()


def test_gateway_recovery_ignores_orphan_response_projection():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        cfg = AgentConfig(
            model_backend="echo",
            gateway_workspace="gateway",
            local_store_path="local_store/local.db",
            local_store_files_dir="local_store/files",
            local_store_events_path="local_store/events.jsonl",
        )
        agent = SimpleAgent(cfg, root)
        paths = gateway_paths(agent)
        for path in (paths.inbox, paths.processing, paths.done, paths.failed, paths.responses):
            path.mkdir(parents=True, exist_ok=True)

        request_path = paths.processing / "gwreq-duplicate.json"
        write_json_file(
            request_path,
            {
                "id": "gwreq-duplicate",
                "kind": "ask",
                "prompt": "已经有响应的 processing 副本",
                "status": "processing",
                "attempts": 1,
                "lease_started_at": time.time() - 10,
            },
        )
        write_json_file(
            paths.responses / "gwreq-duplicate.json",
            {"id": "gwreq-duplicate", "ok": True, "status": "done", "response": "already done"},
        )

        recovered = recover_gateway_processing_requests(
            paths,
            startup=False,
            max_attempts=2,
            timeout_seconds=1,
            agent=agent,
        )
        assert recovered["checked"] == 1
        assert recovered["archived"] == 0
        assert recovered["requeued"] == 1
        assert not request_path.exists()
        assert not (paths.done / request_path.name).exists()
        assert (paths.inbox / request_path.name).exists()
        assert not (paths.failed / request_path.name).exists()


def test_file_adapter_writes_gateway_response_to_outbox():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        cfg = AgentConfig(
            model_backend="echo",
            gateway_workspace="gateway",
            adapter_workspace="adapter",
            local_store_path="local_store/local.db",
            local_store_files_dir="local_store/files",
            local_store_events_path="local_store/events.jsonl",
        )
        agent = SimpleAgent(cfg, root)
        gpaths = gateway_paths(agent)
        apaths = AdapterPaths(
            root=root / "adapter",
            inbox=root / "adapter" / "inbox",
            processing=root / "adapter" / "processing",
            done=root / "adapter" / "done",
            failed=root / "adapter" / "failed",
            outbox=root / "adapter" / "outbox",
        )
        apaths.inbox.mkdir(parents=True, exist_ok=True)
        write_json_file(
            apaths.inbox / "msg-1.json",
            {"id": "msg-1", "text": "文件 adapter 测试", "conversation_id": "conv-1"},
        )

        thread = threading.Thread(
            target=_process_gateway_once_when_inbox_ready,
            args=(agent, gpaths),
        )
        thread.start()
        processed = process_file_adapter_once(
            agent,
            gateway_paths_obj=gpaths,
            adapter_paths_obj=apaths,
            timeout=5,
        )
        thread.join(timeout=5)

        assert processed == 1
        output = json.loads((apaths.outbox / "msg-1.json").read_text(encoding="utf-8"))
        assert output["ok"] is True
        assert output["adapter_message_id"] == "msg-1"
        assert output["gateway_request_id"].startswith(("gw-", "gwreq-"))
