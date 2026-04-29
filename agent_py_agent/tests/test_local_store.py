from __future__ import annotations

import json
import tempfile
from pathlib import Path

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
