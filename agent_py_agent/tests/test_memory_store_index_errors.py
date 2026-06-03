from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

from agent_py_agent.agent.memory_store.jsonl import JsonlMemory


def test_memory_search_report_keeps_fallback_and_reports_index_failure(tmp_path: Path) -> None:
    store = MagicMock()
    store.search.side_effect = ValueError("local memory index is broken")
    memory = JsonlMemory(tmp_path / "memory.jsonl", local_store=store)
    memory.add("user", "Python fallback memory survives index failure")

    results, load_errors = memory.search_report("Python", top_k=5)

    assert [record.content for record in results] == ["Python fallback memory survives index failure"]
    assert load_errors
    assert load_errors[0]["context"] == "memory_store.local_store.search"
    assert "local memory index is broken" in load_errors[0]["message"]


def test_memory_search_legacy_api_still_returns_fallback_on_index_failure(tmp_path: Path) -> None:
    store = MagicMock()
    store.search.side_effect = ValueError("local memory index is broken")
    memory = JsonlMemory(tmp_path / "memory.jsonl", local_store=store)
    memory.add("user", "Legacy search still finds fallback memory")

    results = memory.search("fallback", top_k=5)

    assert [record.content for record in results] == ["Legacy search still finds fallback memory"]
