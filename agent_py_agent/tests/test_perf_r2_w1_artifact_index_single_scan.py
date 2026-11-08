# LLM: PERF-R2-W1 回归：超预算工具索引同一引用的准备与读取只解码一次；索引追加（stat变化）、换引用或负命中都必须重新解码。
# 模块用途: 固定超预算字面查找的单槽复用、失效与整表查找隔离合同。
from __future__ import annotations

import json

import pytest

from agent_py_agent.agent.memory_archive.artifact import reader

REF = "gwreq-msg-perfr2:call_target"
RUN = "gwreq-msg-perfr2"


# LLM: 命中行带run_id作用域，确保prepare走主根精确命中分支而非历史目录发现。
# 函数用途: 写出一个超过测试缓存预算的索引文件。
def _write_index(path):
    lines = [json.dumps({"kind": "tool_call", "scoped_call_id": f"gwreq-msg-other:call_{i}", "path": "", "pad": "x" * 64})
             for i in range(200)]
    lines.insert(50, json.dumps({"kind": "tool_output", "scoped_call_id": REF, "run_id": RUN,
                                 "path": str(path.parent / "out.json")}))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


# LLM: 只计数共享解码器被调用的次数，不改变其输出；缓存预算压到1 KiB使索引必然超预算。
# 函数用途: 返回每次索引解码的预筛参数列表。
@pytest.fixture
def decodes(monkeypatch):
    calls = []
    real = reader.iter_decoded_tool_index_objects

    def counting(source, prescreen=None, **kwargs):
        calls.append(prescreen)
        return real(source, prescreen, **kwargs)

    monkeypatch.setattr(reader, "iter_decoded_tool_index_objects", counting)
    monkeypatch.setattr(reader, "_ARTIFACT_INDEX_CACHE", reader._VerifiedArtifactCache(1024))
    return calls


def test_prepare_decodes_oversized_index_once_for_scoped_ref(tmp_path, decodes):
    root = tmp_path / "owner"
    index = root / "blobs" / "tool_outputs" / "index.jsonl"
    index.parent.mkdir(parents=True)
    _write_index(index)
    request = reader.ReadToolOutputArtifactRequest(root=root, artifact_ref=REF, run_id=RUN)
    prepared = reader.prepare_tool_output_artifact_read(request)
    assert prepared.record is not None and prepared.record["scoped_call_id"] == REF
    assert len(prepared.index_paths) == 1
    assert len(decodes) == 1 and decodes[0] is not None
    assert reader._ARTIFACT_INDEX_CACHE.entries == {}


def test_index_append_or_other_ref_decodes_again(tmp_path, decodes):
    index = tmp_path / "index.jsonl"
    _write_index(index)
    first = reader._parsed_artifact_index(index, literal_ref=REF)
    assert reader._parsed_artifact_index(index, literal_ref=REF) is first
    assert len(decodes) == 1
    with index.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({"kind": "tool_output", "scoped_call_id": REF, "path": "/tmp/new.json"}) + "\n")
    fresh = reader._parsed_artifact_index(index, literal_ref=REF)
    assert len(decodes) == 2 and len(fresh[1][REF]) == 2
    reader._parsed_artifact_index(index, literal_ref="gwreq-msg-other:call_7")
    assert len(decodes) == 3


def test_negative_and_full_lookups_are_never_served_from_slot(tmp_path, decodes):
    index = tmp_path / "index.jsonl"
    _write_index(index)
    absent = "gwreq-msg-absent:call_x"
    reader._parsed_artifact_index(index, literal_ref=absent)
    reader._parsed_artifact_index(index, literal_ref=absent)
    assert len(decodes) == 2
    reader._parsed_artifact_index(index, literal_ref=REF)
    rows, _literals = reader._parsed_artifact_index(index)
    assert len(rows) == 201 and decodes[-1] is None
