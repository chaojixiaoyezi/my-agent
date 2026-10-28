# LLM: 使用真实索引/no-follow读口和独立进程内缓存，正命中可复用解析，负命中不能污染全局LRU或隐藏后继坏行。
# 模块用途: 验证历史索引扫描只保留本次命中的解析，保持完整读盘、缓存隔离及损坏拒绝。
import json

import pytest

from agent_py_agent.agent.memory_archive.artifact import reader


# LLM: 仅隔离缓存实例，文件与查找/解析保持真实；不降低产品预算或截断索引内容。
# 函数用途: 建立一份可被字面或scoped引用命中的磁盘索引。
@pytest.fixture
def index_cache(tmp_path, monkeypatch):
    cache = reader._VerifiedArtifactCache(reader._ARTIFACT_INDEX_CACHE.max_bytes)
    monkeypatch.setattr(reader, '_ARTIFACT_INDEX_CACHE', cache)
    index = tmp_path / 'blobs/tool_outputs/index.jsonl'
    index.parent.mkdir(parents=True)
    index.write_text(json.dumps({'kind': 'tool_output', 'call_id': 'stored',
        'scoped_call_id': 'run:stored', 'run_id': 'run',
        'path': str(index.with_name('body.json')), 'size_bytes': 7}) + '\n')
    return tmp_path, index, cache


# LLM: 两种逻辑引用都必须完整读取索引；不命中不留解析树，避免千根历史噪声驱逐真正热命中。
# 函数用途: 复现冷负命中逐个填满LRU导致驻留内存增长。
@pytest.mark.parametrize('reference', ['missing', 'run:missing'])
def test_literal_index_miss_does_not_fill_positive_cache(index_cache, reference):
    root, _index, cache = index_cache
    plan = reader.prepare_tool_output_artifact_read(reader.ReadToolOutputArtifactRequest(root, reference))
    assert plan.record is None
    assert not cache.entries
    assert cache.bytes == 0


# LLM: 正命中仍按真实FD/stat失效；返回修改不污染内部记录，沿原严格UTF-8拒绝后继损坏而不改变宽容坏JSON策略。
# 函数用途: 守住缓存收益、结果隔离和索引严格解码的原合同。
@pytest.mark.parametrize('reference', ['stored', 'run:stored'])
def test_positive_cache_reuse_still_rejects_changed_index(index_cache, reference):
    root, index, cache = index_cache
    request = reader.ReadToolOutputArtifactRequest(root, reference)
    first = reader.prepare_tool_output_artifact_read(request)
    assert first.record['call_id'] == 'stored'
    assert len(cache.entries) == 1
    first.record['call_id'] = 'caller mutation'
    second = reader.prepare_tool_output_artifact_read(request)
    assert second.record['call_id'] == 'stored'
    assert len(cache.entries) == 1
    with index.open('ab') as stream:
        stream.write(b'\xff\n')
    with pytest.raises(UnicodeDecodeError):
        reader.prepare_tool_output_artifact_read(request)
