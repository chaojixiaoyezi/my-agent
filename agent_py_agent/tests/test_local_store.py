from __future__ import annotations

import json
import tempfile
from pathlib import Path

from agent_py_agent.__main__ import _handle_gateway_request, gateway_paths, submit_gateway_ask
from agent_py_agent.agent.config import AgentConfig
from agent_py_agent.agent.core import SimpleAgent
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
