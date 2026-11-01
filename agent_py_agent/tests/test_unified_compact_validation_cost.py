"""单趟精确校验的操作计数；不能靠截短窗口或概率判重加速。"""
from dataclasses import replace
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.conversation.compact import _validated_compact_source_context
from agent_py_agent.agent.conversation.compact_guard import ConversationCompactError
from agent_py_agent.agent.conversation.compact_scope import THREAD_COMPACT_SCOPE, CompactScope
from agent_py_agent.agent.conversation.compact_summary_view import (
    AppliedCompactContext,
    compact_covered_message_ids,
    resolve_compact_summary_view,
)
from agent_py_agent.agent.conversation.models import MessageLogEntry
from agent_py_agent.tests.test_compact_scoped_checkpoint import compact_case as compact_case


class CountedRows:
    def __init__(self, entries):
        self.entries = entries
        self.passes = self.visits = self.closed = 0

    def __len__(self):
        return len(self.entries)

    def __iter__(self):
        self.passes += 1
        try:
            for row in self.entries:
                self.visits += 1
                yield row
        finally:
            self.closed += 1


def case_source(case, count):
    thread = case.store.threads.require(case.thread_id)
    context = AppliedCompactContext(thread.thread_id, THREAD_COMPACT_SCOPE,
        resolve_compact_summary_view(case.agent, thread, THREAD_COMPACT_SCOPE))
    rows = CountedRows([MessageLogEntry(f'm-{i}', thread.thread_id, 'user', 'body') for i in range(count)])
    return thread, SimpleNamespace(messages=rows, compact_context=context)


@pytest.mark.parametrize('count', [100, 1000, 10000])
def test_source_guard_exactly_one_pass_for_all_cardinalities(compact_case, count):
    thread, source = case_source(compact_case, count)
    assert _validated_compact_source_context(compact_case.agent, thread, source) is source.compact_context
    assert (source.messages.passes, source.messages.visits, source.messages.closed) == (1, count, 1)


@pytest.mark.parametrize('bad', ['duplicate', 'foreign', 'empty_id', 'wrong_id', 'wrong_type'])
@pytest.mark.parametrize('position', [0, 999])
def test_early_and_tail_invalid_rows_are_rejected_and_closed(compact_case, bad, position):
    thread, source = case_source(compact_case, 1000)
    row = source.messages.entries[position]
    if bad == 'duplicate':
        # 第一条重复项仍须在后面的真正重复位置被拒，不缩小校验窗口。
        source.messages.entries[position] = source.messages.entries[1 if position == 0 else 0]
    elif bad == 'foreign':
        source.messages.entries[position] = replace(row, thread_id='another-thread')
    elif bad == 'empty_id':
        source.messages.entries[position] = replace(row, message_id='')
    elif bad == 'wrong_id':
        source.messages.entries[position] = replace(row, message_id=123)
    else:
        source.messages.entries[position] = object()
    with pytest.raises(ConversationCompactError) as failure:
        _validated_compact_source_context(compact_case.agent, thread, source)
    assert failure.value.code == 'COMPACT_SOURCE_CHANGED'
    assert source.messages.passes == source.messages.closed == 1


def test_mutable_source_and_changed_scope_never_reuse_old_validation(compact_case):
    thread, source = case_source(compact_case, 1000)
    _validated_compact_source_context(compact_case.agent, thread, source)
    scope = CompactScope(kind='task', task_id='different', created_at=1.0)
    source.compact_context = AppliedCompactContext(thread.thread_id, scope,
        resolve_compact_summary_view(compact_case.agent, thread, scope))
    _validated_compact_source_context(compact_case.agent, thread, source)
    assert (source.messages.passes, source.messages.visits) == (2, 2000)
    source.messages.entries[-1] = source.messages.entries[0]
    with pytest.raises(ConversationCompactError):
        _validated_compact_source_context(compact_case.agent, thread, source)
    assert source.messages.passes == source.messages.closed == 3


@pytest.mark.parametrize('boundaries,expected', [((), {'m-5'}), (('m-2', 'm-7'), {*(f'm-{i}' for i in range(8))}),
                                              (('missing',), None)])
def test_coverage_projection_one_pass_and_closes(boundaries, expected):
    rows = CountedRows([MessageLogEntry(f'm-{i}', 'thread', 'user', '') for i in range(10)])
    view = SimpleNamespace(source_message_ids={'m-5'}, legacy_message_end_ids=boundaries)
    if expected is None:
        with pytest.raises(OSError, match='boundary'):
            compact_covered_message_ids(view, rows)
    else:
        assert compact_covered_message_ids(view, rows) == frozenset(expected)
    assert (rows.passes, rows.visits, rows.closed) == (1, 10, 1)


def test_legacy_boundary_last_occurrence_keeps_original_prefix_semantics():
    ids = ['a', 'end', 'b', 'end', 'tail']
    rows = CountedRows([MessageLogEntry(value, 'thread', 'user', '') for value in ids])
    view = SimpleNamespace(source_message_ids=(), legacy_message_end_ids=('end',))
    assert compact_covered_message_ids(view, rows) == frozenset({'a', 'end', 'b'})
    assert rows.passes == rows.closed == 1
