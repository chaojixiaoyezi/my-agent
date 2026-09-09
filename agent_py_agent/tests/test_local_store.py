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
from agent_py_agent.agent.gateway_parts.io import gateway_response_path
from agent_py_agent.agent.gateway_parts.queue_service import ensure_gateway_folders
from agent_py_agent.agent.gateway_parts.request_worker import _process_gateway_request_path
from agent_py_agent.agent.io import append_jsonl
from agent_py_agent.agent.local_storage import LocalStore
from agent_py_agent.agent.memory_store import JsonlMemory
from agent_py_agent.agent.settings import AgentConfig
from agent_py_agent.cli.local_doctor import build_local_doctor_report, rebuild_local_store


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

        threads = [
            threading.Thread(target=writer, args=(worker,)) for worker in range(total_threads)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        lines = path.read_text(encoding="utf-8").splitlines()
        records = [json.loads(line) for line in lines]

        assert len(records) == total_threads * per_thread
        assert (
            len({(item["worker"], item["index"]) for item in records}) == total_threads * per_thread
        )


def test_local_store_like_search_when_fts_disabled():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        store = LocalStore(root / "local.db", enable_fts=False)
        store.upsert_record(
            source_type="note",
            source_id="like-source",
            title="like search demo",
            content="FTS5 关闭时也应该能用 LIKE 搜到关键内容。",
        )

        stats = store.stats()
        assert stats["fts5_enabled"] is False
        hits = store.search("关键内容", source_type="note")
        assert len(hits) == 1
        assert hits[0].source_id == "like-source"


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


def test_jsonl_memory_local_store_search_is_scoped_by_memory_path():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        store = LocalStore(root / "local.db")
        first = JsonlMemory(root / "one" / "memory.jsonl", local_store=store)
        second = JsonlMemory(root / "two" / "memory.jsonl", local_store=store)

        first.add("user", "排序算法任务应该留在第一个记忆文件。", kind="dialogue")
        second_hits = second.search("排序算法", top_k=3)

        assert second_hits == []


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
            params=GatewayAskParams(
                prompt="gateway 日志测试：这条请求应该能被搜索到",
                save=False,
                agent=agent,
            ),
        )

        ensure_gateway_folders(paths)
        assert _process_gateway_request_path(agent, paths, request_path, "local-store-test")
        response = read_json_file(gateway_response_path(paths, request_path.stem))
        hits = agent.local_store.search("gateway 日志测试", source_type="gateway_request")

        assert response["ok"] is True
        assert request_id == response["id"]
        assert hits
        assert hits[0].source_id == request_id


def test_gateway_request_writes_runtime_fact_when_saved():
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
            params=GatewayAskParams(
                prompt="gateway runtime fact 测试",
                save=True,
                agent=agent,
            ),
        )

        ensure_gateway_folders(paths)
        assert _process_gateway_request_path(agent, paths, request_path, "local-store-test")
        response = read_json_file(gateway_response_path(paths, request_path.stem))

        assert response["ok"] is True
        fact_path = (
            agent.home_paths.owner_home_dir
            / "memory_archive"
            / "runtime_facts"
            / request_id
            / "task.json"
        )
        assert fact_path.exists()
        payload = json.loads(fact_path.read_text(encoding="utf-8"))
        assert payload["request_id"] == request_id
        assert payload["runtime_progress"]["source"] == "gateway"


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
            params=GatewayAskParams(
                prompt="继续 README",
                save=False,
                include_prompt=True,
                resume_context=True,
                agent=agent,
            ),
        )

        ensure_gateway_folders(paths)
        assert _process_gateway_request_path(agent, paths, request_path, "local-store-test")
        response = read_json_file(gateway_response_path(paths, request_path.stem))

        assert response["ok"] is True
        assert response["memory_resume_context_injected"] is True
        assert response["memory_resume_context_token_estimate"] > 0
        assert "### Auto Recovery Context" in response["prompt"]


def test_local_rebuild_indexes_memory_gateway_and_subagents():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        cfg = AgentConfig(
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
