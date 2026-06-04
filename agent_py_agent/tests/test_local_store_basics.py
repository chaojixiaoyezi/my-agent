"""LLM: Basic local store tests — records, search, timeline, memory, subagent and gateway indexing.

新手说明:
这个文件测试 LocalStore 的基础功能：记录的增删查、事件日志、
LIKE 回退搜索、时间线过滤，以及 JsonlMemory、子代理运行和
简单 Gateway 请求对 LocalStore 的索引联动。
"""

from __future__ import annotations

import json
import tempfile
import threading
from pathlib import Path

from agent_py_agent.agent.core import SimpleAgent
from agent_py_agent.agent.gateway_parts import (
    GatewayAskParams,
    _handle_gateway_request,
    gateway_paths,
    submit_gateway_ask,
)
from agent_py_agent.agent.io import append_jsonl
from agent_py_agent.agent.local_storage import LocalStore
from agent_py_agent.agent.memory_store import JsonlMemory
from agent_py_agent.agent.settings import AgentConfig


def test_local_store_records_events_and_searches():
    """LLM: Verify upsert_record writes content file, logs event, and search returns hit.

    新手说明:
    测试 LocalStore 的基本写入和搜索：插入一条偏好记录后，
    确认记录 ID 以 "rec-" 开头，内容文件存在，搜索能命中，
    事件日志有对应条目。
    """
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
    """LLM: Verify append_jsonl is thread-safe under concurrent writers.

    新手说明:
    测试多线程并发写 JSONL 不会丢行或出现残行：8 个线程各写
    25 条，最终应有 200 条完整记录，且 (worker, index) 对无重复。
    """
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


def test_local_store_like_search_when_fts_disabled():
    """LLM: Verify LIKE-based search works when FTS5 is disabled.

    新手说明:
    测试在 FTS5 关闭的情况下，LocalStore 使用 LIKE 搜索仍然能正确找到包含关键词的记录。
    """
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
    """LLM: Verify timeline() filters by source_type and event_type.

    新手说明:
    测试时间线过滤功能：插入 gateway_request 和 subagent_run 两种事件后，
    按 source_type 或 event_type 过滤能正确返回对应条目。
    """
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
    """LLM: Verify JsonlMemory.add indexes into LocalStore and search works.

    新手说明:
    测试 JsonlMemory 的 add 方法会自动把记忆写入 LocalStore，
    之后用 memory.search 也能通过 LocalStore 找到。
    """
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
    """LLM: Verify index_all backfills existing JSONL records into LocalStore.

    新手说明:
    测试在 JsonlMemory 已有数据但 LocalStore 为空时，
    调用 index_all 可以把旧记录补建索引到 LocalStore。
    """
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
    """LLM: Verify subagent create_run and run_subagent index into LocalStore.

    新手说明:
    测试子代理的 create_run 会写入 subagent_run 类型的记录，
    run_subagent (dry_run) 会写入 subagent_runner_result 和
    subagent_execution_context 类型的记录，全部可通过 LocalStore 搜索到。
    """
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
    """LLM: Verify gateway request handling indexes into LocalStore.

    新手说明:
    测试 gateway 请求处理后会写入 gateway_request 类型的记录到
    LocalStore，且可通过 LocalStore 的 search 搜索到。
    """
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

        response = _handle_gateway_request(agent, request_path)
        hits = agent.local_store.search("gateway 日志测试", source_type="gateway_request")

        assert response["ok"] is True
        assert request_id == response["id"]
        assert hits
        assert hits[0].source_id == request_id


def test_gateway_request_writes_runtime_fact_when_saved():
    """LLM: Verify saved gateway request writes a runtime fact source.

    新手说明:
    测试当 gateway 请求带 save=True 时，会写
    memory_archive/runtime_facts/<request_id>/task.json 作为 compact/resume 事实源。
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
        request_id, request_path, _ = submit_gateway_ask(
            paths,
            params=GatewayAskParams(
                prompt="gateway runtime fact 测试",
                save=True,
                agent=agent,
            ),
        )

        response = _handle_gateway_request(agent, request_path)

        assert response["ok"] is True
        fact_path = agent.home_paths.owner_home_dir / "memory_archive" / "runtime_facts" / request_id / "task.json"
        assert fact_path.exists()
        payload = json.loads(fact_path.read_text(encoding="utf-8"))
        assert payload["request_id"] == request_id
        assert payload["runtime_progress"]["source"] == "gateway"


def test_gateway_request_can_override_resume_context():
    """LLM: Verify gateway request with resume_context=True injects auto-recovery context.

    新手说明:
    测试当 gateway 请求带 resume_context=True 时，响应中
    memory_resume_context_injected 为 True，并且 prompt 中
    包含 "### Auto Recovery Context" 段落。
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

        response = _handle_gateway_request(agent, request_path)

        assert response["ok"] is True
        assert response["memory_resume_context_injected"] is True
        assert response["memory_resume_context_token_estimate"] > 0
        assert "### Auto Recovery Context" in response["prompt"]
