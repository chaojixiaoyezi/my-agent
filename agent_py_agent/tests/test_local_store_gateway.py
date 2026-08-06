"""LLM: Gateway-specific local store tests — recovery, heartbeat, stale detection, file adapter.

新手说明:
这个文件测试 Gateway 相关的 LocalStore 功能：请求恢复与重排、
lease heartbeat 心跳续期、stale 检测与归档、文件适配器转发。
这些场景都依赖 Gateway 的文件目录结构和超时机制。
"""

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
    _process_gateway_requests,
    gateway_paths,
    gateway_stale_processing,
    process_file_adapter_once,
    read_json_file,
    recover_gateway_processing_requests,
    submit_gateway_ask,
    write_json_file,
)
from agent_py_agent.agent.settings import AgentConfig
from agent_py_agent.cli.local_doctor import build_local_doctor_report, rebuild_local_store


def test_local_rebuild_indexes_memory_gateway_and_subagents():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        cfg = AgentConfig(
            tool_protocol="text",
            model_backend="echo",
            my_agent_home=str(root / "home"),
            memory_path="memory.jsonl",
            gateway_workspace="gateway",
            subagent_workspace="subs",
            local_store_path="local_store/local.db",
            local_store_files_dir="local_store/files",
            local_store_events_path="local_store/events.jsonl",
        )
        agent = SimpleAgent(cfg, root)
        agent.remember("重建测试记忆：local-rebuild 应该重新索引。", kind="fact")
        task = agent.subagents.create_run(
            goal="local-rebuild 子代理索引测试",
            thought="生成一个可被重建扫描到的工单。",
            plan=["创建工单", "重建索引"],
        )
        gpaths = gateway_paths(agent)
        submit_gateway_ask(
            gpaths,
            params=GatewayAskParams(
                prompt="local-rebuild gateway 请求索引测试",
                save=False,
                agent=agent,
            ),
        )
        assert _process_gateway_requests(agent, gpaths) == 1

        agent.local_store.reset()
        result = rebuild_local_store(
            agent, sources={"memory", "gateway", "subagent", "fts"}, reset=False
        )

        counts = result["source_counts"]
        assert counts["memory"] == 1
        assert counts["gateway_request"] >= 1
        assert counts["subagent_run"] == 1
        assert (
            agent.local_store.search("local-rebuild 子代理", source_type="subagent_run")[
                0
            ].source_id
            == task.id
        )

        doctor = build_local_doctor_report(agent)
        assert doctor["memory_count"] == 1
        assert any(item["name"] == "memory_index" for item in doctor["checks"])


# ── Shared gateway fixture helpers ─────────────────────────────────────────────


def _setup_agent_with_gateway(
    cfg_overrides: dict | None = None,
) -> tuple[Path, SimpleAgent, AdapterPaths]:
    """Create agent with gateway workspace and all directory paths created."""
    temp_dir = tempfile.TemporaryDirectory()
    root = Path(temp_dir.name)
    cfg = AgentConfig(
        tool_protocol="text",
        model_backend="echo",
        gateway_workspace="gateway",
        local_store_path="local_store/local.db",
        local_store_files_dir="local_store/files",
        local_store_events_path="local_store/events.jsonl",
        **(cfg_overrides or {}),
    )
    agent = SimpleAgent(cfg, root)
    # Keep the temp dir alive for the whole test via the agent instance.
    agent._test_temp_dir = temp_dir  # type: ignore[attr-defined]
    paths = gateway_paths(agent)
    for path in (paths.inbox, paths.processing, paths.done, paths.failed, paths.responses):
        path.mkdir(parents=True, exist_ok=True)
    return root, agent, paths


def _submit_idle_request(paths, prompt: str) -> tuple[str, Path, Path]:
    return submit_gateway_ask(paths, params=GatewayAskParams(prompt=prompt, save=False))


def _write_processing_request(paths, request_id: str, attempts: int, lease_age: float = 10) -> Path:
    path = paths.processing / f"{request_id}.json"
    write_json_file(
        path,
        {
            "id": request_id,
            "kind": "ask",
            "prompt": f"request {request_id}",
            "attempts": attempts,
            "lease_started_at": time.time() - lease_age,
        },
    )
    return path


def _assert_requeued(paths, recovered: dict, request_path: Path) -> None:
    assert recovered["requeued"] == 1
    assert (paths.inbox / request_path.name).exists()


def _assert_failed(paths, recovered: dict, request_path: Path, request_id: str) -> None:
    assert recovered["failed"] == 1
    assert (paths.failed / request_path.name).exists()
    assert (paths.responses / f"{request_id}.json").exists()


def _track_heartbeat_during_run(agent: SimpleAgent, paths, request_path: Path) -> list[float]:
    """Poll lease_heartbeat_at before and during a slow run, return [initial, updated]."""
    observed: list[float] = []
    original_run = agent.run
    processing_path = paths.processing / request_path.name

    def slow_run(user_prompt: str, **kwargs) -> AgentRunResult:
        deadline = time.time() + 2
        initial = 0.0
        while time.time() < deadline:
            initial = float(read_json_file(processing_path).get("lease_heartbeat_at", 0) or 0)
            if initial:
                break
            time.sleep(0.02)
        updated = initial
        deadline = time.time() + 2
        while time.time() < deadline:
            time.sleep(0.05)
            updated = float(read_json_file(processing_path).get("lease_heartbeat_at", 0) or 0)
            if updated > initial:
                break
        observed.extend([initial, updated])
        return AgentRunResult(
            prompt=f"prompt: {user_prompt}", response="ok", backend="test", used_memories=0
        )

    agent.run = slow_run  # type: ignore[method-assign]
    try:
        _process_gateway_requests(agent, paths, worker_id="test-worker")
    finally:
        agent.run = original_run  # type: ignore[method-assign]
    return observed


def _assert_heartbeat_refreshed(observed: list[float], paths, request_path, request_id) -> None:
    archived = read_json_file(paths.done / request_path.name)
    response = read_json_file(paths.responses / f"{request_id}.json")
    assert observed[0] > 0
    assert observed[1] > observed[0]
    assert archived["status"] == "done"
    assert archived["lease_owner"] == "test-worker"
    assert archived["lease_heartbeat_at"] >= observed[1]
    assert response["ok"] is True
    assert response["lease_heartbeat_at"] >= observed[1]


def _process_gateway_once_when_inbox_ready(agent, gpaths) -> None:
    deadline = time.time() + 5
    while time.time() < deadline:
        if list(gpaths.inbox.glob("*.json")):
            _process_gateway_requests(agent, gpaths)
            return
        time.sleep(0.05)


def test_gateway_processing_recovery_requeues_then_fails_after_attempt_limit():
    """LLM: Verify recovery requeues timed-out requests and fails after max attempts."""
    root, agent, paths = _setup_agent_with_gateway(
        {"gateway_processing_timeout_seconds": 1, "gateway_request_max_attempts": 2}
    )

    # First request: requeue (attempts=1, lease timed out)
    request_path = _write_processing_request(paths, "gwreq-timeout", attempts=1)
    recovered = recover_gateway_processing_requests(
        paths, startup=False, max_attempts=2, timeout_seconds=1, agent=agent
    )
    _assert_requeued(paths, recovered, request_path)

    # Second request: fail (attempts=2, lease timed out, exhausted)
    second_path = _write_processing_request(paths, "gwreq-fail", attempts=2)
    failed = recover_gateway_processing_requests(
        paths, startup=False, max_attempts=2, timeout_seconds=1, agent=agent
    )
    _assert_failed(paths, failed, second_path, "gwreq-fail")


def test_gateway_processing_recovery_zero_attempt_limit_is_unlimited():
    root, agent, paths = _setup_agent_with_gateway(
        {"gateway_processing_timeout_seconds": 1, "gateway_request_max_attempts": 0}
    )

    request_path = _write_processing_request(paths, "gwreq-unlimited", attempts=99)
    recovered = recover_gateway_processing_requests(
        paths, startup=False, max_attempts=0, timeout_seconds=1, agent=agent
    )

    _assert_requeued(paths, recovered, request_path)


def test_gateway_worker_refreshes_processing_lease_heartbeat_during_long_run():
    """LLM: Verify gateway worker refreshes lease heartbeat during long agent runs."""
    root, agent, paths = _setup_agent_with_gateway(
        {"gateway_heartbeat_interval": 1, "gateway_processing_timeout_seconds": 1}
    )
    request_id, request_path, _ = submit_gateway_ask(
        paths, params=GatewayAskParams(prompt="长任务 lease heartbeat 测试", save=False)
    )
    observed = _track_heartbeat_during_run(agent, paths, request_path)
    _assert_heartbeat_refreshed(observed, paths, request_path, request_id)


def test_gateway_recovery_uses_lease_heartbeat_before_started_at():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        cfg = AgentConfig(
            tool_protocol="text",
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


def test_gateway_recovery_archives_processing_duplicate_when_response_exists():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        cfg = AgentConfig(
            tool_protocol="text",
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
        assert recovered["archived"] == 1
        assert not request_path.exists()
        assert (paths.done / request_path.name).exists()
        assert not (paths.inbox / request_path.name).exists()
        assert not (paths.failed / request_path.name).exists()


def test_file_adapter_writes_gateway_response_to_outbox():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        cfg = AgentConfig(
            tool_protocol="text",
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
