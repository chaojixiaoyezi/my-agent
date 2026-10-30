# LLM: 临时账本验证真实读取和追加入口；坏orphan不忽略，候选超限不能污染旧提交head。
# 模块用途: 检查点读取保留原提交合同，同时释放未采用的历史正文。
from __future__ import annotations

import hashlib
import json
import tracemalloc
from dataclasses import replace
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.conversation.compact_scope import THREAD_COMPACT_SCOPE
from agent_py_agent.agent.conversation.compact_summary_view import resolve_compact_summary_view
from agent_py_agent.tests.test_compact_scoped_checkpoint import (
    _checkpoint_path,
)
from agent_py_agent.tests.test_compact_scoped_checkpoint import (
    compact_case as _compact_case_fixture,
)

compact_case = _compact_case_fixture


@pytest.mark.parametrize('compatible', [False, True])
def test_model_switch_restores_summary_body_and_evidence_from_base(compact_case, compatible):
    case = compact_case
    thread = _ledger(case, committed=50, body_size=131_072)
    path = _checkpoint_path(case)
    rows = [json.loads(line) for line in path.read_text().split('\n') if line]
    for row in rows[1:]:
        row['provider_compaction'] = {
            'protocol': 'openai_responses', 'endpoint': 'https://provider.invalid/v1',
            'item': {'type': 'compaction', 'encrypted_content': 'synthetic-cipher'},
        }
    path.write_text(''.join(json.dumps(row) + '\n' for row in rows))
    del rows
    case.agent.backend = SimpleNamespace(provider_compaction_scope=lambda: {
        'protocol': 'openai_responses' if compatible else 'anthropic',
        'endpoint': 'https://provider.invalid/v1',
    })
    tracemalloc.start()
    try:
        view = resolve_compact_summary_view(case.agent, thread, THREAD_COMPACT_SCOPE)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    expected = 50 if compatible else 1
    assert view.generation == expected and view.checkpoint_id == f'legacy-{expected}'
    assert view.summary.startswith(f'摘要-{expected - 1}')
    assert view.operation_evidence == {'index': expected - 1}
    assert view.legacy_message_end_ids == tuple(f'message-{index}' for index in range(expected))
    assert bool(view.provider_compaction) is compatible
    assert peak < 2_000_000, f'换模型读取保留了所有摘要: peak={peak}'


# LLM: 仅在pytest临时账本构造明确v1链及orphan，不调用writer绕过真实运行提交；thread以测试快照显式给reader。
# 函数用途: 生成大量旧摘要，测读取本身的峰值而不是测试建样本的内存。
def _ledger(case, *, committed=1, orphans=0, body_size=0):
    path = _checkpoint_path(case)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('w', encoding='utf-8') as handle:
        for index in range(committed + orphans):
            summary = f'摘要-{index}\u0085\u2028' + 'x' * body_size
            row = {
                'checkpoint_id': f'legacy-{index + 1}', 'previous_checkpoint_id': f'legacy-{index}' if index else '',
                'schema': 'conversation_compact_checkpoint.v1', 'generation': index + 1,
                'thread_id': case.thread_id, 'source_kind': 'transcript',
                'source_end_message_id': f'message-{index}', 'created_at': 10.0 + index,
                'summary': summary, 'summary_sha256': hashlib.sha256(summary.encode()).hexdigest(),
                'operation_evidence': {'index': index},
            }
            handle.write(json.dumps(row, ensure_ascii=False) + '\n')
    return replace(case.store.threads.require(case.thread_id), compact_checkpoint_id=f'legacy-{committed}', compact_generation=committed)


@pytest.mark.parametrize('committed,orphans', [(1, 49), (50, 0)])
def test_summary_view_does_not_retain_all_checkpoint_bodies(compact_case, committed, orphans):
    case = compact_case
    thread = _ledger(case, committed=committed, orphans=orphans, body_size=131_072)
    tracemalloc.start()
    try:
        view = resolve_compact_summary_view(case.agent, thread, THREAD_COMPACT_SCOPE)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert view.generation == committed and view.operation_evidence == {'index': committed - 1}
    assert view.summary.endswith('x' * 131_072)
    assert view.legacy_message_end_ids == tuple(f'message-{index}' for index in range(committed))
    print(f'checkpoint memory committed={committed} orphans={orphans} peak_bytes={peak}')
    assert peak < 2_000_000, f'读取保留了整份历史摘要: peak={peak}'


@pytest.mark.parametrize('bad', [b'{', b'[]\n', b'null\n', b'\xff\n'])
def test_bad_orphan_is_not_hidden_by_committed_head(compact_case, bad):
    thread = _ledger(compact_case)
    with _checkpoint_path(compact_case).open('ab') as handle:
        handle.write(bad)
    with pytest.raises(OSError, match='ledger is unreadable'):
        resolve_compact_summary_view(compact_case.agent, thread, THREAD_COMPACT_SCOPE)


@pytest.mark.parametrize('ending', [b'\n', b'\r\n', b''])
def test_blank_lines_duplicate_last_wins_and_complete_unterminated_row(compact_case, ending):
    from agent_py_agent.agent.conversation.compact_checkpoint import (
        committed_compact_checkpoint_chain,
    )

    case = compact_case
    thread = _ledger(case)
    path = _checkpoint_path(case)
    first = path.read_bytes()
    row = json.loads(first)
    row['summary'] = '最后的摘要\u0085\u2028'
    row['summary_sha256'] = hashlib.sha256(row['summary'].encode()).hexdigest()
    blank = '\u00a0\u2028\r\n'.encode()
    path.write_bytes(blank + first + blank + json.dumps(row, ensure_ascii=False).encode() + ending)
    assert committed_compact_checkpoint_chain(case.agent, thread) == (row,)
    view = resolve_compact_summary_view(case.agent, thread, THREAD_COMPACT_SCOPE)
    assert view.summary == row['summary'] and view.legacy_message_end_ids == ('message-0',)


@pytest.mark.parametrize('change', ['replace', 'truncate'])
def test_scanned_selected_row_changed_before_lookup_is_rejected(compact_case, change):
    from agent_py_agent.agent.conversation.compact_checkpoint_scan import checkpoint_row_lookup

    _ledger(compact_case)
    path = _checkpoint_path(compact_case)
    with checkpoint_row_lookup(path) as lookup:
        raw = path.read_bytes()
        if change == 'replace':
            assert b'message-0' in raw
            path.write_bytes(raw.replace(b'message-0', b'message-X'))
        else:
            path.write_bytes(raw[:10])
        with pytest.raises(OSError, match='source changed'):
            lookup('legacy-1')


def test_late_append_is_outside_fixed_snapshot(compact_case):
    from agent_py_agent.agent.conversation.compact_checkpoint_scan import checkpoint_row_lookup

    _ledger(compact_case)
    path = _checkpoint_path(compact_case)
    with checkpoint_row_lookup(path) as lookup:
        expected = lookup('legacy-1')
        with path.open('ab') as handle:
            handle.write(b'{')
        assert lookup('legacy-1') == expected
    with pytest.raises(OSError, match='ledger is unreadable'):
        with checkpoint_row_lookup(path):
            pass


def test_unselected_committed_summary_still_validated_and_file_closed(compact_case, monkeypatch):
    from pathlib import Path

    from agent_py_agent.agent.conversation.compact_scope import CompactScope
    from agent_py_agent.tests.test_compact_scoped_checkpoint import _commit_transcript

    case = compact_case
    _commit_transcript(case, scope=CompactScope(kind='turn', turn_id='old-private'),
                       summary='private', content='private', publish=False)
    case.clock[0] = 20.0
    thread, _, _ = _commit_transcript(case, scope=THREAD_COMPACT_SCOPE,
                                     summary='public', content='public', publish=True)
    path = _checkpoint_path(case)
    rows = [json.loads(line) for line in path.read_text().split('\n') if line]
    rows[0]['summary'] = 'corrupt'
    path.write_text(''.join(json.dumps(row) + '\n' for row in rows))
    handles = []
    original_open = Path.open

    def capture(self, *args, **kwargs):
        handle = original_open(self, *args, **kwargs)
        if self == path:
            handles.append(handle)
        return handle

    monkeypatch.setattr(Path, 'open', capture)
    with pytest.raises(OSError, match='summary digest'):
        resolve_compact_summary_view(case.agent, thread, THREAD_COMPACT_SCOPE)
    assert handles and all(handle.closed for handle in handles)


def test_empty_head_does_not_read_orphan_ledger(compact_case):
    case = compact_case
    _ledger(case)
    _checkpoint_path(case).write_bytes(b'{')
    thread = case.store.threads.require(case.thread_id)
    assert resolve_compact_summary_view(case.agent, thread, THREAD_COMPACT_SCOPE).checkpoint_id == ''


def test_missing_ledger_with_committed_head_is_not_empty_view(compact_case):
    thread = _ledger(compact_case)
    _checkpoint_path(compact_case).unlink()
    with pytest.raises(OSError, match='chain is incomplete'):
        resolve_compact_summary_view(compact_case.agent, thread, THREAD_COMPACT_SCOPE)


# LLM: 物理行上限包含UTF8编码和末尾LF；真实追加入口必须在任何磁盘写入之前拒绝超限候选。
# 函数用途: 覆盖恰好8MiB与多一字节的边界，核对旧提交head始终可读。
@pytest.mark.parametrize('extra', [0, 1])
def test_checkpoint_append_admission_uses_reader_physical_row_budget(compact_case, extra):
    from agent_py_agent.agent.conversation.compact_checkpoint import (
        _append_checkpoint,
        committed_compact_checkpoint_chain,
        scoped_compact_checkpoint_id,
    )
    from agent_py_agent.agent.conversation.compact_checkpoint_scan import _CHECKPOINT_ROW_MAX_BYTES

    case = compact_case
    thread = _ledger(case)
    path = _checkpoint_path(case)
    original_bytes = path.read_bytes()
    expected = committed_compact_checkpoint_chain(case.agent, thread)
    row = json.loads(original_bytes)
    row.update(schema='conversation_compact_checkpoint.v3', generation=2,
               previous_checkpoint_id='legacy-1', summary='界\n\\"')
    row['checkpoint_id'] = scoped_compact_checkpoint_id(row)
    base_bytes = len((json.dumps(row, ensure_ascii=False, sort_keys=True) + '\n').encode('utf-8'))
    row['summary'] += 'x' * (_CHECKPOINT_ROW_MAX_BYTES - base_bytes + extra)
    row['checkpoint_id'] = scoped_compact_checkpoint_id(row)
    physical_bytes = len((json.dumps(row, ensure_ascii=False, sort_keys=True) + '\n').encode('utf-8'))
    assert physical_bytes == _CHECKPOINT_ROW_MAX_BYTES + extra
    if extra:
        with pytest.raises(ValueError, match='COMPACT_CHECKPOINT_RECORD_TOO_LARGE') as caught:
            _append_checkpoint(case.agent, row)
        assert caught.value.code == 'COMPACT_CHECKPOINT_RECORD_TOO_LARGE'
        assert caught.value.row_bytes == physical_bytes
        assert caught.value.max_row_bytes == _CHECKPOINT_ROW_MAX_BYTES
        assert path.read_bytes() == original_bytes
    else:
        assert _append_checkpoint(case.agent, row) == row['checkpoint_id']
        assert path.stat().st_size == len(original_bytes) + physical_bytes
    assert committed_compact_checkpoint_chain(case.agent, thread) == expected


# LLM: 缩小地址预算但完整消费真实提交链；旁听原解析和物理读取，不用耗时阈值代替复杂度断言。
# 函数用途: 防止尾部head和长前驱链每缺一个地址就重扫全账本。
@pytest.mark.parametrize('committed', [8, 40])
def test_full_chain_over_index_budget_has_constant_stream_scans(compact_case, monkeypatch, committed):
    from collections import OrderedDict
    from pathlib import Path

    from agent_py_agent.agent.conversation import compact_checkpoint_scan as scan
    from agent_py_agent.agent.conversation.compact_checkpoint import (
        iter_committed_compact_checkpoints,
    )
    from agent_py_agent.tests.test_opt_compact_chunk_scan import CountedRaw

    case = compact_case
    thread = _ledger(case, committed=committed)
    path = _checkpoint_path(case)
    monkeypatch.setattr(scan, '_CHECKPOINT_OFFSETS', OrderedDict())
    monkeypatch.setattr(scan, '_CHECKPOINT_INDEX_MAX_ROWS', 2)
    counts = {'readinto': [], 'readline': 0}
    parsed = []
    original_open, original_parse = Path.open, scan._parse_row

    # LLM: 真实描述符的读取全部转交原实现，只收集scanner的块扫描次数。
    # 函数用途: 区分有界随机读和逐ID全量流扫。
    def opened(self, *args, **kwargs):
        handle = original_open(self, *args, **kwargs)
        return CountedRaw(handle, counts) if self == path else handle

    # LLM: 原解析继续验证每条JSON，不伪造提交链或缺失记录。
    # 函数用途: 为同一入口保留可重复的总解析工作量断言。
    def counted(raw):
        parsed.append(len(raw))
        return original_parse(raw)

    monkeypatch.setattr(Path, 'open', opened)
    monkeypatch.setattr(scan, '_parse_row', counted)
    rows = tuple(iter_committed_compact_checkpoints(case.agent, thread))
    assert [row['checkpoint_id'] for row in rows] == [f'legacy-{i}' for i in range(committed, 0, -1)]
    assert sum(counts['readinto']) <= 2 * path.stat().st_size
    assert len(parsed) <= 3 * committed
