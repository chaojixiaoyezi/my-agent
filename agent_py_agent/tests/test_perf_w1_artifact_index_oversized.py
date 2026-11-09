# LLM: PERF-W1 回归：超缓存预算的工具索引不得整表物化或做驻留体积遍历，字面命中结果须与整表解析完全一致。
# 模块用途: 固定超预算索引的预筛查找、转义行兜底与候选预算合同。
from __future__ import annotations

import json

import pytest

from agent_py_agent.agent.memory_archive.artifact import reader

REF = "gwreq-msg-perfw1:call_target"


# LLM: 行内容覆盖普通命中、无关行与JSON转义写法的命中行，验证字节预筛不漏行。
# 函数用途: 写出一个超过测试缓存预算的索引文件。
def _write_index(path):
    lines = [json.dumps({"kind": "tool_call", "scoped_call_id": f"gwreq-msg-other:call_{i}", "path": "", "pad": "x" * 64})
             for i in range(200)]
    lines.insert(50, json.dumps({"kind": "tool_output", "scoped_call_id": REF, "path": "/tmp/a.txt"}))
    escaped = json.dumps({"kind": "tool_output", "call_id": "call_target", "path": "/tmp/b.txt"}).replace(
        '"call_id": "call_target"', '"scoped_call_id": "gwreq\\u002dmsg-perfw1:call_target"')
    lines.insert(120, escaped)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


@pytest.fixture
def oversized_index(tmp_path, monkeypatch):
    index = tmp_path / "index.jsonl"
    _write_index(index)
    monkeypatch.setattr(reader, "_ARTIFACT_INDEX_CACHE", reader._VerifiedArtifactCache(1024))
    return index


def test_oversized_literal_lookup_matches_full_parse_without_size_walk(oversized_index, monkeypatch):
    full_rows, full_literals = reader._parsed_artifact_index(oversized_index)  # literal_ref=None: full parse
    measure = reader._retained_tree_bytes

    def measure_candidates(value, seen):
        candidate_rows = value[1][1][0]
        assert len(candidate_rows) == 2  # 计量仅限真命中，不遍历200条历史噪声。
        return measure(value, seen)

    monkeypatch.setattr(reader, "_retained_tree_bytes", measure_candidates)
    rows, literals = reader._parsed_artifact_index(oversized_index, literal_ref=REF)
    assert literals[REF] == full_literals[REF]
    assert len(literals[REF]) == 2  # 普通命中 + 转义写法命中
    assert len(rows) < len(full_rows)
    assert reader._ARTIFACT_INDEX_CACHE.entries == {}


def test_oversized_physical_lookup_keeps_all_rows_and_skips_cache(oversized_index, monkeypatch):
    monkeypatch.setattr(reader, "_retained_tree_bytes", lambda *_a, **_k: pytest.fail("size walk on oversized index"))
    rows, _literals = reader._parsed_artifact_index(oversized_index)
    assert len(rows) == 202
    assert reader._ARTIFACT_INDEX_CACHE.entries == {}


def test_unsafe_literal_falls_back_to_full_decode(oversized_index):
    unsafe = 'ref"with-quote'
    rows, literals = reader._parsed_artifact_index(oversized_index, literal_ref=unsafe)
    assert len(rows) == 202 and unsafe not in literals
