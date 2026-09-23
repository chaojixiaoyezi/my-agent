"""检查点读取保留原提交合同，同时释放未采用的历史正文。"""
from __future__ import annotations

import hashlib
import json
import tracemalloc
from dataclasses import replace

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
