"""审计 #11 修复真测:记忆 JSONL 逐行容错——单坏行/未知字段/非 dict 不再崩掉整条记忆召回。

真写一个含坏 JSON 行、未知顶层字段行(模拟新版本写的记忆)、非 dict 行、好行的文件,断言坏的被跳过、
好的保留(未知字段过滤),整体不抛异常(多版本滚动升级的记忆数据可用性)。
"""

from __future__ import annotations

import json

from agent_py_agent.agent.memory_store.jsonl import JsonlMemory


def test_read_memory_file_skips_corrupt_lines(tmp_path) -> None:
    mem = JsonlMemory(tmp_path / "mem.jsonl")
    mem.path.write_text(
        "\n".join([
            json.dumps({"role": "user", "content": "good1"}),
            "{not valid json",  # 坏 JSON 行(并发撕行/磁盘满半写)
            json.dumps({"role": "assistant", "content": "good2", "future_field": 123}),  # 新版本未知字段
            json.dumps([1, 2, 3]),  # 非 dict
            "",  # 空行
            json.dumps({"role": "user", "content": "good3"}),
        ]),
        encoding="utf-8",
    )
    records = mem._read_memory_file(mem.path)  # 不抛异常
    assert [r.content for r in records] == ["good1", "good2", "good3"]  # 坏的跳过、好的全保留
    assert all(r.role for r in records)  # 未知字段被过滤,记录正常构造


def test_record_missing_required_field_skipped(tmp_path) -> None:
    mem = JsonlMemory(tmp_path / "mem.jsonl")
    mem.path.write_text(
        "\n".join([
            json.dumps({"content": "no role"}),  # 缺必填 role → 跳过(构造失败)
            json.dumps({"role": "user", "content": "ok"}),
        ]),
        encoding="utf-8",
    )
    records = mem._read_memory_file(mem.path)
    assert [r.content for r in records] == ["ok"]  # 缺必填的被跳过,不崩


def test_missing_file_returns_empty(tmp_path) -> None:
    mem = JsonlMemory(tmp_path / "nope.jsonl")
    assert mem._read_memory_file(mem.path) == []
