# LLM: 使用临时owner真实writer/CAS和不可变索引验证覆盖消费者及prepare工具防线；禁止用小窗口冒充历史或按yield数量漏计枚举。
# 模块用途: 回归超过近期窗口的消息/工具重复来源、视图比较与增量消费。
from __future__ import annotations

from copy import deepcopy
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.conversation import compact_checkpoint as checkpoints
from agent_py_agent.agent.conversation.compact import _validated_compact_source_context
from agent_py_agent.agent.conversation.compact_guard import ConversationCompactError
from agent_py_agent.agent.conversation.compact_scope import CompactScope
from agent_py_agent.agent.conversation.compact_summary_view import (
    AppliedCompactContext,
    _IndexedMessageCoverage,
    _IndexedToolCoverage,
    resolve_compact_summary_view,
)
from agent_py_agent.agent.conversation.models import ConversationCompactCommit
from agent_py_agent.agent.conversation.store_io import ImmutableJsonIndex
from agent_py_agent.tests.test_compact_scoped_checkpoint import compact_case as compact_case


# LLM: 真实提交301条task来源，避免全线程物理轮转影响重复来源断言；上下文绑定原提交头。
# 函数用途: 构造最早消息已离开256窗口的可读摘要。
def _covered_context(case):
    before = case.store.threads.require(case.thread_id)
    rows = tuple(case.store.messages.append({'thread_id': case.thread_id, 'role': 'user', 'content': str(i)})
                 for i in range(301))
    scope = CompactScope(kind='task', task_id='coverage', created_at=5.0)
    checkpoint_id = checkpoints.write_compact_checkpoint(case.agent, checkpoints.CompactCheckpointRequest(
        thread=before, summary='覆盖301条', operation_evidence={}, compact_rows=rows, retained_tail=(),
        source_end_byte_offset=0, projected_tokens_before=900, projected_tokens_after=300,
        policy=case.policy, forced=False, scope=scope,
    ))
    thread = case.store.threads.update_compact_state(case.thread_id, commit=ConversationCompactCommit(
        summary='覆盖301条', operation_evidence={}, checkpoint_id=checkpoint_id,
        compacted_through_message_id=rows[-1].message_id, compacted_through_byte_offset=0,
        source_messages=len(rows), source_tool_pairs=0, publish_thread_view=False,
    ), expected_generation=0)
    view = resolve_compact_summary_view(case.agent, thread, scope)
    return thread, rows, AppliedCompactContext(thread.thread_id, scope, view)


# LLM: 调用真实摘要前guard；最早旧ID必须被拒绝，不以近期集合的C层运算代替冷根查询。
# 函数用途: 防止301条覆盖后的首条消息被重复压缩。
def test_old_covered_id_is_rejected_by_real_source_guard(compact_case):
    thread, rows, context = _covered_context(compact_case)
    assert rows[0].message_id in context.view.source_message_ids
    source = SimpleNamespace(messages=(rows[0],), compact_context=context)
    with pytest.raises(ConversationCompactError, match='repeats covered'):
        _validated_compact_source_context(compact_case.agent, thread, source)


# LLM: 只记录全索引枚举，精确get仍走真实磁盘；比较两个相同根不得枚举历史。
# 函数用途: 验证反向交集与相等比较在窗口外仍准确且有界。
def test_indexed_message_operators_and_view_equality_do_not_enumerate(tmp_path, monkeypatch):
    index = ImmutableJsonIndex(tmp_path, ('index',))
    root = None
    for i in range(301):
        root = index.put(root, str(i), True)
    coverage = _IndexedMessageCoverage(tuple(str(i) for i in range(45, 301)), index, root, 301)
    visits = []
    original = index.items

    # LLM: 保留原迭代结果，只统计真实冷条目，不放行错误返回值。
    # 函数用途: 发现隐藏在集合操作中的全量展开。
    def counted(reference):
        for row in original(reference):
            visits.append(row[0])
            yield row

    monkeypatch.setattr(index, 'items', counted)
    assert {'0'} & coverage == frozenset({'0'})
    assert coverage & {'0'} == frozenset({'0'})
    assert not coverage.isdisjoint({'0'})
    assert coverage == deepcopy(coverage)
    assert visits == []
    # 显式全集运算允许读冷表，但标准Set工厂不能调用需要根参数的领域构造器。
    assert coverage | {'new'} == frozenset({*(str(i) for i in range(301)), 'new'})


# LLM: 工具根有129条，近期仅128；空增量仍调用真实恢复分区和原生刷新，不能枚举旧覆盖。
# 函数用途: 回归实际工具热消费者的零全表遍历合同。
@pytest.mark.parametrize('entrypoint', ['recovery', 'native'])
def test_tool_consumers_do_not_enumerate_historical_coverage(tmp_path, monkeypatch, entrypoint):
    from agent_py_agent.agent.agent_core._tool_loop_service import _refresh_native_compact_context
    from agent_py_agent.agent.agent_core.compact_tool_partition import (
        partition_recovery_tool_source,
    )
    from agent_py_agent.agent.conversation.compact_summary_view import CompactSummaryView

    index = ImmutableJsonIndex(tmp_path, ('tools',))
    import json

    refs = tuple({'run_id': 'r', 'attempt_id': 'a', 'turn_id': 't', 'call_id': str(i)} for i in range(129))
    root = None
    for i, ref in enumerate(refs):
        root = index.put(root, json.dumps(ref, sort_keys=True), {'ref': ref, 'order': i})
    coverage = _IndexedToolCoverage(refs[-128:], index, root, len(refs))
    visits = []
    original = index.items

    # LLM: 原索引枚举不替换数据，只计数实际读取的历史条目。
    # 函数用途: 捕捉list、星号解包或比较带来的全量遍历。
    def counted(reference):
        for row in original(reference):
            visits.append(row[0])
            yield row

    monkeypatch.setattr(index, 'items', counted)
    if entrypoint == 'recovery':
        assert partition_recovery_tool_source((), (), covered_tool_refs=coverage) is None
        assert partition_recovery_tool_source((refs[0],), (), covered_tool_refs=coverage) is None
    else:
        scope = CompactScope(kind='turn', turn_id='t')
        params = SimpleNamespace(compact_context=AppliedCompactContext('thread', scope, CompactSummaryView(
            checkpoint_id='old', summary='old', source_tool_refs=coverage,
        )))
        new = {'run_id': 'r', 'attempt_id': 'a', 'turn_id': 't', 'call_id': 'new'}
        _refresh_native_compact_context(params, SimpleNamespace(compact_checkpoint_id='new', compact_generation=2),
                                        summary='new', source_refs=(new,))
        assert refs[0] in params.compact_context.view.source_tool_refs
        assert new in params.compact_context.view.source_tool_refs
        assert coverage.with_added((refs[-128],)) == coverage
    assert visits == []


# LLM: 使用真实live writer及CAS连续提交129条和1条工具，刷新后必须等于同head的权威恢复视图。
# 函数用途: 防止native只刷新refs却丢失封印元数据，导致下一次来源一致性检查误报。
@pytest.mark.parametrize('first_native', [False, True])
def test_native_refresh_matches_committed_carried_view(compact_case, first_native):
    from agent_py_agent.agent.agent_core._tool_loop_service import _refresh_native_compact_context
    from agent_py_agent.agent.conversation.live_tool_compact import (
        LiveToolCompactBinding,
        LiveToolCompactCommitRequest,
        commit_live_tool_compact,
    )

    case = compact_case
    scope = CompactScope(kind='turn', turn_id='native-turn')

    # LLM: 所有工具引用和摘要沿产品writer/CAS提交，不手造提交后root。
    # 函数用途: 为两次真实原生刷新建立同一scope的可读基础。
    def commit(thread, refs, summary):
        return commit_live_tool_compact(case.agent, LiveToolCompactBinding(store=case.store, thread=thread, scope=scope),
            LiveToolCompactCommitRequest(summary=summary, source_tool_call_ids=tuple(ref['call_id'] for ref in refs),
                retained_tool_call_ids=(), projected_tokens_before=900, projected_tokens_after=300, policy=case.policy,
                request_id='request', attempt_id='attempt', source_tool_refs=refs))

    refs = tuple({'run_id': 'r', 'attempt_id': 'a', 'turn_id': 't', 'call_id': str(i)} for i in range(129))
    initial = case.store.threads.require(case.thread_id)
    first = commit(initial, refs, 'first')
    before = initial if first_native else first
    view = resolve_compact_summary_view(case.agent, before, scope)
    params = SimpleNamespace(compact_context=AppliedCompactContext(case.thread_id, scope, view))
    added = ({'run_id': 'r', 'attempt_id': 'a', 'turn_id': 't', 'call_id': 'new'},)
    current = first if first_native else commit(first, added, 'second')
    _refresh_native_compact_context(params, current, summary='first' if first_native else 'second',
                                    source_refs=refs if first_native else added,
                                    agent=case.agent)
    assert params.compact_context.view == resolve_compact_summary_view(case.agent, current, scope)


# LLM: 两个有效历史根共享128条近期工具，但更早的第一个来源不同；原生tuple的ne不可冒充全历史比较。
# 函数用途: 使持久工具覆盖的!=精确等于==的否定。
def test_tool_inequality_includes_cold_root(tmp_path):
    import json

    index = ImmutableJsonIndex(tmp_path, ('tools',))
    common = tuple({'run_id': 'r', 'attempt_id': 'a', 'turn_id': 't', 'call_id': str(i)} for i in range(128))
    roots = []
    for first_id in ('first-a', 'first-b'):
        refs = ({'run_id': 'r', 'attempt_id': 'a', 'turn_id': 't', 'call_id': first_id}, *common)
        root = None
        for order, ref in enumerate(refs):
            root = index.put(root, json.dumps(ref, sort_keys=True), {'ref': ref, 'order': order})
        roots.append(_IndexedToolCoverage(common, index, root, len(refs)))
    same = roots[0] == roots[1]
    assert not same
    assert roots[0] != roots[1]


# LLM: 真实live writer/CAS提交129条工具，最早引用已离开128热窗口；真实prepare须拒绝重复来源且绝不枚举冷表。
# 函数用途: 为工具重复防线补独立回归，防止只查近期窗口或关掉guard后仍全绿。
def test_old_covered_tool_is_rejected_without_index_enumeration(compact_case, monkeypatch):
    from agent_py_agent.agent.conversation.active_turn_compact import CarriedToolCompactSource
    from agent_py_agent.agent.conversation.compact import (
        ConversationCompactOptions,
        ConversationCompactSource,
        prepare_conversation_context,
    )
    from agent_py_agent.agent.conversation.live_tool_compact import (
        LiveToolCompactBinding,
        LiveToolCompactCommitRequest,
        commit_live_tool_compact,
    )

    case = compact_case
    scope = CompactScope(kind='turn', turn_id='guard-turn')
    refs = tuple({'run_id': 'r', 'attempt_id': 'a', 'turn_id': 't', 'call_id': str(i)} for i in range(129))
    thread = commit_live_tool_compact(case.agent,
        LiveToolCompactBinding(store=case.store, thread=case.store.threads.require(case.thread_id), scope=scope),
        LiveToolCompactCommitRequest(summary='已覆盖129条工具', source_tool_call_ids=tuple(ref['call_id'] for ref in refs),
            retained_tool_call_ids=(), projected_tokens_before=900, projected_tokens_after=300, policy=case.policy,
            request_id='request', attempt_id='attempt', source_tool_refs=refs))
    view = resolve_compact_summary_view(case.agent, thread, scope)
    assert isinstance(view.source_tool_refs, _IndexedToolCoverage)
    assert len(view.source_tool_refs) == 129
    assert refs[0] not in tuple.__iter__(view.source_tool_refs)
    calls = []
    original = ImmutableJsonIndex.items

    # LLM: 统计调用本身而非yield条数；原索引结果不替换，零调用断言能发现空表枚举。
    # 函数用途: 捕获来源校验和视图重载中任何全表读取。
    def counted(self, reference):
        calls.append(reference)
        return original(self, reference)

    # LLM: 正常重复来源必须在投影或摘要之前拒绝；若guard被移除则显式失败，不把其它异常当拒绝通过。
    # 函数用途: 确认拒绝来自真实来源防线而不是预算或假后端。
    def unexpected_projection(_view):
        pytest.fail('重复工具来源不得进入请求投影')

    monkeypatch.setattr(ImmutableJsonIndex, 'items', counted)
    source = ConversationCompactSource(thread, (), case.policy, {}, compact_context=AppliedCompactContext(thread.thread_id, scope, view))
    tools = CarriedToolCompactSource(source_records=(dict(refs[0], output='旧工具正文'),), retained_records=(),
        source_tool_refs=(refs[0],), retained_tool_refs=())
    with pytest.raises(ConversationCompactError) as failure:
        prepare_conversation_context(case.agent, case.store, thread, options=ConversationCompactOptions(
            source=source, force=True, request_projector=unexpected_projection, tool_source=tools))
    assert failure.value.code == 'COMPACT_SOURCE_CHANGED'
    assert calls == []
