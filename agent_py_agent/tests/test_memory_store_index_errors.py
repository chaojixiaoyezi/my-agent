from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

from agent_py_agent.agent.memory_store.jsonl import JsonlMemory


# LLM: 正式召回只读owner内MemoryReadIndex，旧LocalStore不再参与；真实损坏已建索引文件，不伪造读取结果。
# 函数用途: 验证读索引不可用时仍回读正式权威，并把降级如实报告而不是当作没有记忆。
def test_memory_search_report_reads_primary_jsonl_and_reports_index_failure(tmp_path: Path) -> None:
    from agent_py_agent.agent.memory_store.read_index import MemoryReadIndex

    store = MagicMock()
    store.search.side_effect = ValueError("local memory index is broken")
    memory = JsonlMemory(tmp_path / "memory.jsonl", local_store=store)
    memory.add("user", "Python primary JSONL memory survives index failure")
    assert memory.index_all() == 1
    MemoryReadIndex(memory.path).path.write_bytes(b"local memory index is broken" * 64)

    results, load_errors = memory.search_report("Python", top_k=5)

    assert [record.content for record in results] == ["Python primary JSONL memory survives index failure"]
    assert load_errors
    assert load_errors[0]["context"] == "memory_store.read_index"
    assert load_errors[0]["coverage"]["status"] == "unavailable"
    assert load_errors[0]["fallback"] == "committed_authority_scan"
    store.search.assert_not_called()


def test_memory_search_api_reads_primary_jsonl_on_index_failure(tmp_path: Path) -> None:
    store = MagicMock()
    store.search.side_effect = ValueError("local memory index is broken")
    memory = JsonlMemory(tmp_path / "memory.jsonl", local_store=store)
    memory.add("user", "Search still finds primary JSONL memory")

    results = memory.search("primary", top_k=5)

    assert [record.content for record in results] == ["Search still finds primary JSONL memory"]
