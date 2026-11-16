# LLM: 所有来源和派生数据库都位于pytest临时owner中；用真实JSONL、SQLite与分段摘要，不经writer登记追加见证。
# 模块用途: 验证前缀复核对整段一次读全的分段用hashlib核对，被截开的分段仍走可续摘要，改写仍被发现。
from __future__ import annotations

import json

from agent_py_agent.agent.local_storage import owner_read_index as module
from agent_py_agent.agent.local_storage.owner_read_index import OwnerReadIndex
from agent_py_agent.agent.observability.telemetry_ledger import ToolCallRecord

PARTS = ('data', 'telemetry', 'tool_calls.jsonl')


# LLM: 复用真实遥测记录格式，身份与时间字段稳定。
# 函数用途: 生成一条有效遥测样本。
def _row(index):
    return ToolCallRecord(call_id=f'call-{index}', tool_name='tool', run_id=f'run-{index}',
                          owner_id='owner', started_at=float(index + 1), ended_at=float(index + 1),
                          duration_ms=float(index)).to_dict()


# LLM: 先完整投影一次，再绕过writer直接追加一行，迫使下一次推进复核整个已发布前缀；清空进程缓存模拟冷进程。
# 函数用途: 准备一个需要前缀复核的来源并返回首次视图。
def _projected_then_appended(home, count=60):
    path = home.joinpath(*PARTS)
    path.parent.mkdir(parents=True)
    path.write_text(''.join(json.dumps(_row(index)) + '\n' for index in range(count)))
    first = OwnerReadIndex(home).advance('telemetry', PARTS)
    assert first['status'] == 'complete' and first['through'] > 16 * 1024
    with path.open('a') as stream:
        stream.write(json.dumps(_row(count)) + '\n')
    module._PROCESS_MEMOS.clear()
    module._PROCESS_APPENDS.clear()
    return path, first


# LLM: 纯Python摘要每MiB约1秒，空闲网关每次追加都复核10 MB前缀就是持续CPU；整段读全时不得再走它。
# 函数用途: 禁用纯Python压缩函数后，追加后的前缀复核仍完成且保持原代次。
def test_whole_span_prefix_proof_does_not_use_pure_python_sha256(tmp_path, monkeypatch):
    _path, first = _projected_then_appended(tmp_path)

    def refuse(*_args, **_kwargs):
        raise AssertionError('pure-python sha256 used for a whole span')

    monkeypatch.setattr(module, '_sha256_compress', refuse)
    index = OwnerReadIndex(tmp_path)
    view = index.advance('telemetry', PARTS)

    assert view['status'] == 'complete', view
    assert view['generation'] == first['generation']
    assert index.query(view)['total_count'] == 61


# LLM: 读块小于分段时仍按可续摘要逐块核对，跨实例小预算续算的既有合同不变。
# 函数用途: 把读块压到100字节，核对被截开的分段照样证明完成。
def test_split_span_reads_still_prove_with_resumable_hasher(tmp_path, monkeypatch):
    _path, first = _projected_then_appended(tmp_path)
    monkeypatch.setattr(module, '_OWNER_INDEX_CHUNK_BYTES', 100)

    for _ in range(80):
        index = OwnerReadIndex(tmp_path)
        view = index.advance('telemetry', PARTS, max_bytes=4096)
        if view['status'] == 'complete':
            break
    assert view['status'] == 'complete', view
    assert view['generation'] == first['generation']
    assert index.query(view)['total_count'] == 61


# LLM: hashlib快路径与原摘要逐位一致，等长改写旧前缀必须让复核失败并换代次，不能凭长度增长当成追加。
# 函数用途: 核对快路径仍发现首段等长改写。
def test_fast_path_still_detects_same_length_prefix_rewrite(tmp_path):
    path, first = _projected_then_appended(tmp_path)
    path.write_bytes(path.read_bytes().replace(b'call-0', b'call-X', 1))

    index = OwnerReadIndex(tmp_path)
    view = index.advance('telemetry', PARTS)

    assert view['status'] == 'complete', view
    assert view['generation'] != first['generation']
    assert index.query(view, filters={'call_id': 'call-X'})['total_count'] == 1
