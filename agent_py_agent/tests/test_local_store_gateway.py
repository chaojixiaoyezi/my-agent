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

from agent_py_agent.__main__ import (
    AdapterPaths,
    _process_gateway_requests,
    build_local_doctor_report,
    gateway_paths,
    gateway_stale_processing,
    process_file_adapter_once,
    read_json_file,
    rebuild_local_store,
    recover_gateway_processing_requests,
    submit_gateway_ask,
    write_json_file,
)
from agent_py_agent.agent.agent_core.models import AgentRunResult
from agent_py_agent.agent.config import AgentConfig
from agent_py_agent.agent.core import SimpleAgent


def test_local_rebuild_indexes_memory_gateway_and_subagents():
    """LLM: Verify rebuild_local_store re-indexes memory, gateway, and subagent sources.

    新手说明:
    测试 rebuild_local_store 在 reset 后能从 memory、gateway_request、
    subagent_run 三个来源重建索引，并且 build_local_doctor_report
    返回正确的计数和检查项。
    """
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        cfg = AgentConfig(
            model_backend="echo",
            memory_path="memory.jsonl",
            gateway_workspace="gateway",
            subagent_workspace="subs",
            local_store_path="local_store/local.db",
            local_store_files_dir="local_store/files",
            local_store_events_path="local_store/events.jsonl",
        )
        agent = SimpleAgent(cfg, root)
        agent.remember("重建测试记忆：local-rebuild 应该重新索引。", kind="note")
        task = agent.subagents.create_run(
            goal="local-rebuild 子代理索引测试",
            thought="生成一个可被重建扫描到的工单。",
            plan=["创建工单", "重建索引"],
        )
        gpaths = gateway_paths(agent)
        submit_gateway_ask(
            gpaths,
            prompt="local-rebuild gateway 请求索引测试",
            save=False,
            agent=agent,
        )
        assert _process_gateway_requests(agent, gpaths) == 1

        agent.local_store.reset()
        result = rebuild_local_store(agent, sources={"memory", "gateway", "subagent", "fts"}, reset=False)

        counts = result["source_counts"]
        assert counts["memory"] == 1
        assert counts["gateway_request"] >= 1
        assert counts["subagent_run"] == 1
        assert agent.local_store.search("local-rebuild 子代理", source_type="subagent_run")[0].source_id == task.id

        doctor = build_local_doctor_report(agent)
        assert doctor["memory_count"] == 1
        assert any(item["name"] == "memory_index" for item in doctor["checks"])


def test_gateway_processing_recovery_requeues_then_fails_after_attempt_limit():
    """LLM: Verify recovery requeues timed-out requests and fails after max attempts.

    新手说明:
    测试 gateway 请求恢复机制：超时未完成的请求会被重新放回 inbox
    （requeued），而超过最大尝试次数的请求会被归档到 failed 目录
    并生成失败响应。
    """
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
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
        paths = gateway_paths(agent)
        for path in (paths.inbox, paths.processing, paths.done, paths.failed, paths.responses):
            path.mkdir(parents=True, exist_ok=True)

        request_path = paths.processing / "gwreq-timeout.json"
        write_json_file(
            request_path,
            {
                "id": "gwreq-timeout",
                "kind": "ask",
                "prompt": "会被重排的请求",
                "attempts": 1,
                "lease_started_at": time.time() - 10,
            },
        )
        recovered = recover_gateway_processing_requests(
            paths,
            startup=False,
            max_attempts=2,
            timeout_seconds=1,
            agent=agent,
        )
        assert recovered["requeued"] == 1
        assert (paths.inbox / request_path.name).exists()

        second_path = paths.processing / "gwreq-fail.json"
        write_json_file(
            second_path,
            {
                "id": "gwreq-fail",
                "kind": "ask",
                "prompt": "会失败归档的请求",
                "attempts": 2,
                "lease_started_at": time.time() - 10,
            },
        )
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


def test_gateway_worker_refreshes_processing_lease_heartbeat_during_long_run():
    """LLM: Verify gateway worker refreshes lease heartbeat during long agent runs.

    新手说明:
    测试 gateway worker 在长时间运行的 agent.run 执行过程中
    会持续刷新 processing 目录下请求文件的 lease_heartbeat_at
    时间戳，防止被 recovery 误判为超时。
    """
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
        request_id, request_path, _ = submit_gateway_ask(paths, prompt="长任务 lease heartbeat 测试", save=False)
        observed: list[float] = []
        original_run = agent.run

        def slow_run(user_prompt: str, **kwargs) -> AgentRunResult:
            processing_path = paths.processing / request_path.name
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

        archived = read_json_file(paths.done / request_path.name)
        response = read_json_file(paths.responses / f"{request_id}.json")
        assert observed[0] > 0
        assert observed[1] > observed[0]
        assert archived["status"] == "processing"
        assert archived["lease_owner"] == "test-worker"
        assert archived["lease_heartbeat_at"] >= observed[1]
        assert response["ok"] is True
        assert response["lease_heartbeat_at"] >= observed[1]


def test_gateway_recovery_uses_lease_heartbeat_before_started_at():
    """LLM: Verify recovery checks lease_heartbeat_at before lease_started_at for staleness.

    新手说明:
    测试 gateway 恢复机制优先看 lease_heartbeat_at 而不是
    lease_started_at：当 heartbeat 时间戳仍然新鲜时，即使
    started_at 已超时，也不应该判定为 stale。
    """
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


def test_gateway_recovery_archives_processing_duplicate_when_response_exists():
    """LLM: Verify recovery archives processing request when matching response already exists.

    新手说明:
    测试当 processing 目录下有一个请求，但 responses 目录下
    已经有对应的响应文件时，recovery 应该直接把 processing
    中的请求归档到 done，而不是重排或标记失败。
    """
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
        assert recovered["archived"] == 1
        assert not request_path.exists()
        assert (paths.done / request_path.name).exists()
        assert not (paths.inbox / request_path.name).exists()
        assert not (paths.failed / request_path.name).exists()


def test_file_adapter_writes_gateway_response_to_outbox():
    """LLM: Verify file adapter forwards inbox message to gateway and writes response to outbox.

    新手说明:
    测试文件适配器的端到端流程：把 inbox 中的消息转发给 gateway
    处理，然后把 gateway 的响应写入 outbox，实现与外部系统的
    文件级集成。
    """
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

        def gateway_once():
            deadline = time.time() + 5
            while time.time() < deadline:
                if list(gpaths.inbox.glob("*.json")):
                    _process_gateway_requests(agent, gpaths)
                    return
                time.sleep(0.05)

        thread = threading.Thread(target=gateway_once)
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
        assert output["gateway_request_id"].startswith("gwreq-")
