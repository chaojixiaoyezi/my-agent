from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

from agent_py_agent.agent.memory_store.jsonl import JsonlMemory


def test_memory_search_report_reads_primary_jsonl_and_reports_index_failure(tmp_path: Path) -> None:
    store = MagicMock()
    store.search.side_effect = ValueError("local memory index is broken")
    memory = JsonlMemory(tmp_path / "memory.jsonl", local_store=store)
    memory.add("user", "Python primary JSONL memory survives index failure")

    results, load_errors = memory.search_report("Python", top_k=5)

    assert [record.content for record in results] == ["Python primary JSONL memory survives index failure"]
    assert load_errors
    assert load_errors[0]["context"] == "memory_store.local_store.search"
    assert "local memory index is broken" in load_errors[0]["message"]


def test_memory_search_api_reads_primary_jsonl_on_index_failure(tmp_path: Path) -> None:
    store = MagicMock()
    store.search.side_effect = ValueError("local memory index is broken")
    memory = JsonlMemory(tmp_path / "memory.jsonl", local_store=store)
    memory.add("user", "Search still finds primary JSONL memory")

    results = memory.search("primary", top_k=5)

    assert [record.content for record in results] == ["Search still finds primary JSONL memory"]
