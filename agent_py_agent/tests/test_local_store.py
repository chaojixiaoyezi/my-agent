from __future__ import annotations

import json
import tempfile
import threading
import time
from pathlib import Path

from agent_py_agent.__main__ import (
    AdapterPaths,
    _handle_gateway_request,
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
from agent_py_agent.agent.file_io import append_jsonl
from agent_py_agent.agent.local_store import LocalStore
from agent_py_agent.agent.memory import JsonlMemory


def test_local_store_records_events_and_searches():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        store = LocalStore(root / "local.db")

        record = store.upsert_record(
            source_type="memory",
            source_id="demo-1",
            title="偏好记录",
            content="用户喜欢清晰的表格和可追溯测试证据。",
            metadata={"kind": "preference"},
        )

        assert record.id.startswith("rec-")
        assert (root / "files" / f"{record.id}.txt").exists()
        hits = store.search("表格", source_type="memory")
        assert len(hits) == 1
        assert hits[0].metadata["kind"] == "preference"
        assert "测试证据" in hits[0].content

        stats = store.stats()
        assert stats["record_count"] == 1
        assert stats["event_count"] == 1

        events = (root / "events.jsonl").read_text(encoding="utf-8").splitlines()
        assert len(events) == 1
        assert json.loads(events[0])["event_type"] == "record_upserted"


def test_locked_jsonl_append_preserves_complete_lines_under_threads():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        path = root / "events.jsonl"
        total_threads = 8
        per_thread = 25

        def writer(worker: int) -> None:
            for index in range(per_thread):
                append_jsonl(path, {"worker": worker, "index": index})

        threads = [threading.Thread(target=writer, args=(worker,)) for worker in range(total_threads)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        lines = path.read_text(encoding="utf-8").splitlines()
        records = [json.loads(line) for line in lines]

        assert len(records) == total_threads * per_thread
        assert len({(item["worker"], item["index"]) for item in records}) == total_threads * per_thread


def test_local_store_like_fallback_when_fts_disabled():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        store = LocalStore(root / "local.db", enable_fts=False)
        store.upsert_record(
            source_type="note",
            source_id="fallback",
            title="fallback demo",
            content="FTS5 关闭时也应该能用 LIKE 搜到关键内容。",
        )

        stats = store.stats()
        assert stats["fts5_enabled"] is False
        hits = store.search("关键内容", source_type="note")
        assert len(hits) == 1
        assert hits[0].source_id == "fallback"


def test_local_store_timeline_filters_events():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        store = LocalStore(root / "local.db")
        store.log_record(
            source_type="gateway_request",
            source_id="gw-1",
            title="Gateway demo",
            content="gateway timeline demo",
            metadata={"status": "done"},
            event_type="gateway_request_completed",
        )
        store.log_record(
            source_type="subagent_run",
            source_id="sub-1",
            title="Subagent demo",
            content="subagent timeline demo",
            metadata={"status": "PLANNING"},
            event_type="subagent_run_saved",
        )

        all_items = store.timeline(limit=5)
        gateway_items = store.timeline(limit=5, source_type="gateway_request")
        event_items = store.timeline(limit=5, event_type="subagent_run_saved")

        assert len(all_items) >= 4
        assert len(gateway_items) == 2
        assert {item.source_id for item in gateway_items} == {"gw-1"}
        assert len(event_items) == 1
        assert event_items[0].source_type == "subagent_run"


def test_jsonl_memory_indexes_to_local_store():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        store = LocalStore(root / "local.db")
        memory = JsonlMemory(root / "memory.jsonl", local_store=store)

        memory.add("user", "我希望本地查询先用 SQLite 和 FTS5。", kind="preference")
        hits = memory.search("SQLite", top_k=3)

        assert len(hits) == 1
        assert hits[0].role == "user"
        assert hits[0].kind == "preference"
        assert "FTS5" in hits[0].content


def test_jsonl_memory_can_backfill_existing_records():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        memory_path = root / "memory.jsonl"
        JsonlMemory(memory_path).add("assistant", "旧 JSONL 记录也能补建索引。", kind="note")

        store = LocalStore(root / "local.db")
        memory = JsonlMemory(memory_path, local_store=store)
        assert memory.index_all() == 1
        assert store.stats()["record_count"] == 1
        assert memory.search("补建", top_k=1)[0].role == "assistant"


def test_subagent_flow_indexes_logs_to_local_store():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        cfg = AgentConfig(
            model_backend="echo",
            memory_path="memory.jsonl",
            subagent_workspace="subs",
            local_store_path="local_store/local.db",
            local_store_files_dir="local_store/files",
            local_store_events_path="local_store/events.jsonl",
        )
        agent = SimpleAgent(cfg, root)
        task = agent.subagents.create_run(
            goal="统一日志测试：让子代理记录可以搜索",
            thought="验证 subagent_run 和 runner_result 会进入 LocalStore。",
            plan=["生成工单", "执行 dry-run runner", "搜索日志"],
        )

        run_hits = agent.local_store.search("统一日志测试", source_type="subagent_run")
        assert len(run_hits) == 1
        assert run_hits[0].source_id == task.id

        result = agent.run_subagent(task.id, dry_run=True)
        runner_hits = agent.local_store.search(task.id, source_type="subagent_runner_result")
        context_hits = agent.local_store.search(task.id, source_type="subagent_execution_context")

        assert result.run_id == task.id
        assert runner_hits
        assert context_hits


def test_gateway_request_indexes_logs_to_local_store():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        cfg = AgentConfig(
            model_backend="echo",
            memory_path="memory.jsonl",
            gateway_workspace="gateway",
            local_store_path="local_store/local.db",
            local_store_files_dir="local_store/files",
            local_store_events_path="local_store/events.jsonl",
        )
        agent = SimpleAgent(cfg, root)
        paths = gateway_paths(agent)
        request_id, request_path, _ = submit_gateway_ask(
            paths,
            prompt="gateway 日志测试：这条请求应该能被搜索到",
            save=False,
            agent=agent,
        )

        response = _handle_gateway_request(agent, request_path)
        hits = agent.local_store.search("gateway 日志测试", source_type="gateway_request")

        assert response["ok"] is True
        assert request_id == response["id"]
        assert hits
        assert hits[0].source_id == request_id


def test_gateway_request_writes_recovery_snapshot_when_saved():
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
        request_id, request_path, _ = submit_gateway_ask(
            paths,
            prompt="gateway recovery snapshot 测试",
            save=True,
            agent=agent,
        )

        response = _handle_gateway_request(agent, request_path)

        assert response["ok"] is True
        assert response["recovery_snapshot_path"]
        snapshot_path = Path(response["recovery_snapshot_path"])
        records = [
            json.loads(line)
            for line in snapshot_path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        assert records[-1]["snapshot_id"] == response["recovery_snapshot_id"]
        assert records[-1]["dispatch_events"][0]["source"] == "gateway"
        assert records[-1]["dispatch_events"][0]["request_id"] == request_id


def test_gateway_request_can_override_resume_context():
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
        agent.run("gateway README 恢复开关任务", save=True)
        paths = gateway_paths(agent)
        _, request_path, _ = submit_gateway_ask(
            paths,
            prompt="继续 README",
            save=False,
            include_prompt=True,
            resume_context=True,
            agent=agent,
        )

        response = _handle_gateway_request(agent, request_path)

        assert response["ok"] is True
        assert response["memory_resume_context_injected"] is True
        assert response["memory_resume_context_token_estimate"] > 0
        assert "### Auto Recovery Context" in response["prompt"]


def test_local_rebuild_indexes_memory_gateway_and_subagents():
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
        assert output["gateway_request_id"].startswith(("gw-", "gwreq-"))
