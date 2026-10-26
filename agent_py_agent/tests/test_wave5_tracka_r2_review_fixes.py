# LLM: 只操作tmp_path正式JSONL，检索、信封预算、范围复核与访问刷盘均走产品实现；仅隔离外部决策/工具/恢复服务。
# 模块用途: 验证只有最终注入的正式记忆登记访问，预算淘汰、来源撤销和重复项不产生新访问。
import time
from dataclasses import replace
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.agent_core.runtime import loop_support
from agent_py_agent.agent.agent_core.runtime.loop_models import RuntimeContextRequest
from agent_py_agent.agent.conversation import decision_service
from agent_py_agent.agent.memory_store import JsonlMemory, MemoryRecord, decision_recall
from agent_py_agent.agent.memory_store.recall import (
    MemoryRecallScope,
    long_term_record_matches_scope,
)
from agent_py_agent.agent.prompting_parts.memory_context import (
    BudgetedMemoryRecords,
    memory_context_text,
)
from agent_py_agent.tests.test_decision_recall import install


# LLM: 临时宿主不接真实后端；JSONL与正式scope保持真实，固定来源时间避免预算测量发生漂移。
# 函数用途: 构造本轮正式候选与空HOT/lesson源。
def _host(tmp_path, contents):
    memory = JsonlMemory(tmp_path / 'memory.jsonl')
    for index, content in enumerate(contents):
        memory.add_record(MemoryRecord('user', content, kind='fact', entry_id=f'r4-{index}',
            created_at=100, updated_at=100,
            attributes={'scope_type': 'personal', 'scope_key': 'personal', 'origin': 'user_explicit'}))
    host = SimpleNamespace(memory=memory, memory_hot=SimpleNamespace(list=lambda: []),
        memory_lessons=SimpleNamespace(list=lambda: []),
        backend=SimpleNamespace(name='test', model_name='offline'),
        config=SimpleNamespace(memory_top_k=3, model_context_window_tokens=8000))
    routed = SimpleNamespace(receipts=[], findings=[], injected_sections=[], supplement_entry_ids=[])
    return host, routed, MemoryRecallScope.from_runtime()


# LLM: 预算按真实完整安全信封计算，含遗漏提示；每个样本明确保留前N条，不修改预算算法。
# 函数用途: 为指定最终入选数量设置恰好够用的上下文预算。
def _budget(monkeypatch, rows, keep):
    cap = len(memory_context_text(BudgetedMemoryRecords(rows[:keep], omitted_count=len(rows) - keep)))
    monkeypatch.setattr(loop_support, '_FORMAL_MEMORY_BUDGET_CHARS', cap)
    selected = loop_support._budgeted_formal_memories(rows)
    assert [row.entry_id for row in selected] == [row.entry_id for row in rows[:keep]]


# LLM: spy保留原_note_access副作用，不能拿调用模拟结果代替真实_pending_access。
# 函数用途: 同时观察重复访问调用和原始待刷记录。
def _touches(monkeypatch, memory):
    calls = []
    original = memory._note_access

    # LLM: 不替换真实访问动作，仅在原调用之前记录精确ID。
    # 函数用途: 观察一次访问并执行原副作用。
    def note(entry_id):
        calls.append(entry_id)
        original(entry_id)

    monkeypatch.setattr(memory, '_note_access', note)
    return calls


# LLM: 核对内存与重新打开后的持久访问时间；保留既有pending，不清空它伪造“无新访问”。
# 函数用途: 验证只有最终选择产生一次新访问，未选择且未预访问的行刷盘后仍为零。
def _assert_access(memory, calls, selected, prior=None):
    prior = prior or {}
    selected_ids = [row.entry_id for row in selected]
    assert calls == selected_ids
    assert set(memory._pending_access) == set(selected_ids) | set(prior)
    for entry_id, timestamp in prior.items():
        if entry_id not in selected_ids:
            assert memory._pending_access[entry_id] == timestamp
    expected = dict(memory._pending_access)
    assert memory.flush_access_events() == len(expected)
    assert memory._pending_access == {}
    for row in JsonlMemory(memory.path).all():
        assert row.last_accessed_at == expected.get(row.entry_id, 0)
        assert row.version == 1


# LLM: 仅去掉与召回无关的工具/恢复/上下文包I/O，真实_prepare_runtime_context及所有正式记忆处理仍执行。
# 函数用途: 为基础召回回归隔离不相关运行设施。
def _runtime_services(monkeypatch, routed):
    monkeypatch.setattr(loop_support, '_tool_snapshots_for_run', lambda *_args: (None, None))
    monkeypatch.setattr(loop_support, '_routed_memory_context_for_request', lambda *_args, **_kwargs: routed)
    monkeypatch.setattr(loop_support, '_resume_context_for_request',
                        lambda *_args, **_kwargs: (SimpleNamespace(injected=False), ''))
    monkeypatch.setattr(loop_support, 'build_runtime_main_context_bundle', lambda *_args, **_kwargs: None)


# LLM: 同一真实top-k三条全部命中；检查全淘汰/保留1/2及已有访问不被回滚，不替换检索或预算器。
# 函数用途: 复现基础召回先记访问、后预算淘汰的缺陷。
@pytest.mark.parametrize('keep', [0, 1, 2])
@pytest.mark.parametrize('existing_access', [False, True])
def test_r4_baseline_only_final_budget_admissions_are_touched(tmp_path, monkeypatch, keep, existing_access):
    host, routed, scope = _host(tmp_path, [f'Alpha shared fact {i} ' + 'material ' * 80 for i in range(3)])
    request = RuntimeContextRequest('Alpha shared fact', None, False)
    # LLM: 与产品自动召回使用同一typed scope。
    # 函数用途: 只允许本轮范围内的候选。
    def predicate(row):
        return long_term_record_matches_scope(row, scope)

    candidates = host.memory.search_scoped_candidates(request.user_prompt, 3, predicate, latency_safe=True)
    assert len(candidates) == 3 and host.memory._pending_access == {}
    _budget(monkeypatch, candidates, keep)
    prior = {candidates[-1].entry_id: 7.0} if existing_access else {}
    host.memory._pending_access.update(prior)
    calls = _touches(monkeypatch, host.memory)
    _runtime_services(monkeypatch, routed)

    result = loop_support._prepare_runtime_context(host, request).memories

    assert [row.entry_id for row in result] == [row.entry_id for row in candidates[:keep]]
    assert result.omitted_count == 3 - keep
    _assert_access(host.memory, calls, result, prior)


# LLM: 只替换付费决策边界及其策略读取；scope、两次refresh、FragmentSearch与最终正式源确认全部真实。
# 函数用途: 让离线决策明确选中本轮第二个补充查询片段。
def _supplement_services(monkeypatch, fragment_material='query_text'):
    stage = SimpleNamespace(error_code='', enabled_points=('pre_recall',), deadline=time.monotonic() + 30)
    answer = SimpleNamespace(question_id='supplemental_query', value='query_2', error_code='')
    outcome = SimpleNamespace(mode='apply', status='success', may_apply=True,
                              response=SimpleNamespace(answers=(answer,)))
    monkeypatch.setattr(decision_service, 'begin_decision_stage', lambda *_args, **_kwargs: stage)
    monkeypatch.setattr(decision_recall, '_fragment_material', lambda *_args: fragment_material)
    monkeypatch.setattr(decision_recall, 'decide', lambda *_args, **_kwargs: outcome)
    monkeypatch.setattr(decision_recall, 'decision_outcome_is_current', lambda *_args: True)
    return stage


# LLM: 两条补充事实正文均够小可过片段预算，但完整信封最终只允许0/1/2条；原始基线只用候选读口。
# 函数用途: 复现补充召回在外层最终预算前提前确认访问的缺陷。
@pytest.mark.parametrize('keep_extra', [0, 1, 2])
@pytest.mark.parametrize('fragment_material', ['query_text', 'with_new_facts'])
def test_r4_supplement_only_final_envelope_admissions_are_touched(tmp_path, monkeypatch, keep_extra, fragment_material):
    host, routed, scope = _host(tmp_path, ['Alpha baseline', 'Beta supplement 1', 'Beta supplement 2'])
    request = RuntimeContextRequest('Find Alpha baseline。Find Beta supplement。', None, False,
                                    request_id='request', run_id='run')
    rows = host.memory.all()
    baseline = rows[:1]
    _budget(monkeypatch, rows, keep_extra + 1)
    remaining = loop_support._FORMAL_MEMORY_BUDGET_CHARS - len(memory_context_text(baseline))
    previews = decision_recall.FragmentSearch(host, scope, frozenset([baseline[0].entry_id]), (2, 3, remaining))
    assert {row.entry_id for row in previews.additions('Beta supplement')} == {row.entry_id for row in rows[1:]}
    _supplement_services(monkeypatch, fragment_material)
    calls = _touches(monkeypatch, host.memory)

    result = loop_support._formal_memories_for_request(host, request, routed,
        recall_scope=scope, long_term_memories=baseline, skip_formal_recall=False)

    assert [row.entry_id for row in result] == [row.entry_id for row in rows[:keep_extra + 1]]
    assert 'memory_pre_recall_decision:apply:success' in routed.findings
    _assert_access(host.memory, calls, result)
    assert routed.supplement_entry_ids == [row.entry_id for row in rows[1:keep_extra + 1]]


# LLM: 独立补充调用没有外层预算，因此默认必须继续确认访问；已有基线不得重复touch。
# 函数用途: 守住直接调用supplement_recalled_memories的既有合同。
def test_r4_direct_supplement_still_confirms_only_new_records(tmp_path, monkeypatch):
    host, _routed, scope = _host(tmp_path, ['Alpha baseline', 'Beta supplement 1', 'Beta supplement 2'])
    rows = host.memory.all()
    stage = _supplement_services(monkeypatch)
    request = RuntimeContextRequest('Find Alpha baseline。Find Beta supplement。', None, False)
    calls = _touches(monkeypatch, host.memory)
    result, finding = decision_recall.supplement_recalled_memories(host, request, rows[:1],
        recall_scope=scope, stage=stage, queries=(('query_2', 'Beta supplement'),),
        slots=2, search_top_k=3, remaining_chars=1000, refresh=lambda: host.memory.all()[:1])
    assert [row.entry_id for row in result] == [row.entry_id for row in rows]
    assert finding == 'memory_pre_recall_decision:apply:success'
    _assert_access(host.memory, calls, rows[1:])


# LLM: 固定来源优先于同ID长期候选；正式基线重复项只登记一次，不能把HOT/lesson当作JSONL访问。
# 函数用途: 验证跨正式来源去重完成后才登记访问。
def test_r4_fixed_source_duplicate_does_not_touch_shadowed_long_term(tmp_path, monkeypatch):
    host, routed, scope = _host(tmp_path, ['Alpha shadowed', 'Alpha selected'])
    rows = host.memory.all()
    hot = replace(rows[0], kind='hot', content='fixed HOT authority')
    monkeypatch.setattr(loop_support, 'hot_memory_records', lambda *_args, **_kwargs: [hot])
    calls = _touches(monkeypatch, host.memory)
    result = loop_support._formal_memories_for_request(host, RuntimeContextRequest('Alpha', None, False), routed,
        recall_scope=scope, long_term_memories=[*rows, rows[1]], skip_formal_recall=False)
    assert [row.entry_id for row in result] == [hot.entry_id, rows[1].entry_id]
    assert result[0].kind == 'hot'
    _assert_access(host.memory, calls, rows[1:])


# LLM: 最终预算结束后原JSONL发生变化，确认必须重读原scope/版本/正文；缓存视图不能授权旧候选。
# 函数用途: 验证最终确认前撤销或变更的记录既不注入，也不产生访问。
@pytest.mark.parametrize('change', ['remove', 'scope', 'version'])
def test_r4_final_confirmation_rejects_changed_authority(tmp_path, monkeypatch, change):
    host, routed, scope = _host(tmp_path, ['Alpha old source', 'Alpha stable source'])
    rows = host.memory.all()
    original = loop_support._budgeted_formal_memories
    budgets = []

    # LLM: 第二次预算之后只改临时权威源，模拟最终确认前合法的撤销/换版竞态。
    # 函数用途: 固定来源变化的观察点，不替换原预算结果。
    def budget(records):
        result = original(records)
        budgets.append(True)
        if len(budgets) == 2:
            changes = {'action': 'remove'} if change == 'remove' else (
                {'attributes': {'scope_type': 'company', 'scope_key': 'company:other'}} if change == 'scope'
                else {'version': 2, 'content': 'replacement body'})
            host.memory.path.write_text(replace(rows[0], **changes).to_json() + '\n' + rows[1].to_json() + '\n')
        return result

    monkeypatch.setattr(loop_support, '_budgeted_formal_memories', budget)
    calls = _touches(monkeypatch, host.memory)
    result = loop_support._formal_memories_for_request(host, RuntimeContextRequest('Alpha', None, False), routed,
        recall_scope=scope, long_term_memories=rows, skip_formal_recall=False)
    assert [row.entry_id for row in result] == [rows[1].entry_id]
    assert calls == [rows[1].entry_id]
    pending = dict(host.memory._pending_access)
    assert set(pending) == {rows[1].entry_id}
    assert host.memory.flush_access_events() == 1
    for row in JsonlMemory(host.memory.path).all():
        assert row.last_accessed_at == pending.get(row.entry_id, 0)


# LLM: 正式库的历史kind不是来源分类；HOT/lesson仓库为空时，同名kind的JSONL记录仍需确认访问。
# 函数用途: 防止按kind偷换来源，漏掉真正已注入的长期记录。
@pytest.mark.parametrize('kind', ['hot', 'lesson'])
def test_r4_long_term_kind_is_not_mistaken_for_fixed_repository(tmp_path, monkeypatch, kind):
    host, routed, scope = _host(tmp_path, ['Alpha formal source'])
    row = replace(host.memory.all()[0], kind=kind)
    host.memory.path.write_text(row.to_json() + '\n')
    calls = _touches(monkeypatch, host.memory)
    result = loop_support._formal_memories_for_request(host, RuntimeContextRequest('Alpha', None, False), routed,
        recall_scope=scope, long_term_memories=host.memory.all(), skip_formal_recall=False)
    assert [record.entry_id for record in result] == [row.entry_id]
    _assert_access(host.memory, calls, result)


# LLM: 隔离与owner关闭时，连只读候选/正式源也不能触达；不是“读过但不记访问”的软隔离。
# 函数用途: 保持task_local、control_plane和owner记忆总闸合同。
@pytest.mark.parametrize('mode', ['task_local', 'control_plane', 'owner_off'])
def test_r4_suppressed_runtime_never_reads_or_touches_formal_memory(tmp_path, monkeypatch, mode):
    host, routed, _scope = _host(tmp_path, ['Alpha hidden source'])
    request = RuntimeContextRequest('Alpha', None, False,
                                    context_scope=mode if mode != 'owner_off' else 'default')
    if mode == 'owner_off':
        host.owner_policy = SimpleNamespace(memory_enabled=False)
    _runtime_services(monkeypatch, routed)
    calls = _touches(monkeypatch, host.memory)

    # LLM: 隔离条件成立时，任何正式库读取都属于回归失败。
    # 函数用途: 精确捕获不该发生的正式记忆访问。
    def forbidden(*_args, **_kwargs):
        pytest.fail('suppressed recall cannot read the formal repository')

    for method in ('all', 'search_scoped', 'search_scoped_candidates', 'confirm_scoped_access'):
        monkeypatch.setattr(host.memory, method, forbidden)
    assert loop_support._prepare_runtime_context(host, request).memories == []
    _assert_access(host.memory, calls, [])


# LLM: 真rerank经refresh把被撤销的HOT同ID投影为JSONL；再次撤销JSONL须在最终确认拒绝，不复用初始固定源豁免。
# 函数用途: 复现独立审查指出的来源切换后漏确认，以及切换后再次撤销仍被注入的问题。
@pytest.mark.parametrize('remove_fallback', [False, True])
def test_r4_source_transition_keeps_final_confirmation_authoritative(tmp_path, monkeypatch, remove_fallback):
    host, routed, scope = _host(tmp_path, ['Alpha fallback', 'Alpha one', 'Alpha two'])
    rows = host.memory.all()
    fixed = [replace(rows[0], kind='hot', content='fixed HOT before decision')]
    monkeypatch.setattr(loop_support, 'hot_memory_records', lambda *_args, **_kwargs: list(fixed))
    stage = SimpleNamespace(error_code='', enabled_points=('recall',), deadline=time.monotonic() + 30)
    monkeypatch.setattr(decision_service, 'begin_decision_stage', lambda *_args, **_kwargs: stage)

    # LLM: 只在离线决策返回时撤销固定来源，原rerank的refresh/stale检查仍完整执行。
    # 函数用途: 模拟决策等待期间原HOT已经撤销。
    def revoke_fixed(response):
        fixed.clear()
        return response

    decisions = install(monkeypatch, mutate=revoke_fixed)
    original = loop_support._budgeted_formal_memories
    budgets = []

    # LLM: 先有初选预算、refresh预算，再是最终预算；仅在最后一个观察点可选撤销回落JSONL。
    # 函数用途: 制造来源切换完成后、最终访问确认前的撤销竞态。
    def budget(records):
        result = original(records)
        budgets.append(True)
        if len(budgets) == 3 and remove_fallback:
            host.memory.path.write_text(replace(rows[0], action='remove').to_json() + '\n'
                                        + ''.join(row.to_json() + '\n' for row in rows[1:]))
        return result

    monkeypatch.setattr(loop_support, '_budgeted_formal_memories', budget)
    touches = _touches(monkeypatch, host.memory)
    request = RuntimeContextRequest('Alpha', None, False, request_id='source-transition', run_id='run')
    result = loop_support._formal_memories_for_request(host, request, routed,
        recall_scope=scope, long_term_memories=rows, skip_formal_recall=False)
    expected = rows[1:] if remove_fallback else rows
    assert len(decisions) == 1 and len(budgets) == 3
    assert 'memory_recall_decision:apply:stale' in routed.findings
    assert [row.entry_id for row in result] == [row.entry_id for row in expected]
    _assert_access(host.memory, touches, expected)


# LLM: 新出现但未投影进基线的固定源不能污染补充候选来源；两个材料模式均走真实补充/最终确认。
# 函数用途: 复现刷新扩大固定来源豁免、导致同ID补充JSONL漏访问或撤销后仍注入的问题。
@pytest.mark.parametrize('fragment_material', ['query_text', 'with_new_facts'])
@pytest.mark.parametrize('remove_extra', [False, True])
def test_r4_unselected_fixed_source_cannot_exempt_supplement(tmp_path, monkeypatch, fragment_material, remove_extra):
    host, routed, scope = _host(tmp_path, ['Alpha baseline', 'Beta supplement'])
    rows = host.memory.all()
    fixed = []
    monkeypatch.setattr(loop_support, 'hot_memory_records', lambda *_args, **_kwargs: list(fixed))
    _supplement_services(monkeypatch, fragment_material)
    decide = decision_recall.decide

    # LLM: 决策期间出现一个未被基线选中的同ID固定源；不替换候选检索或refresh。
    # 函数用途: 固定新增来源的竞态观察点。
    def publish_fixed(*args, **kwargs):
        fixed.append(replace(rows[1], kind='hot', content='not selected HOT'))
        return decide(*args, **kwargs)

    monkeypatch.setattr(decision_recall, 'decide', publish_fixed)
    original = loop_support._budgeted_formal_memories
    budgets = []

    # LLM: 初选、两次refresh后为最终预算，此时可选撤销补充JSONL以检验最终源重核。
    # 函数用途: 在最终确认前撤销候选，不改预算算法。
    def budget(records):
        result = original(records)
        budgets.append(True)
        if len(budgets) == 4 and remove_extra:
            host.memory.path.write_text(rows[0].to_json() + '\n' + replace(rows[1], action='remove').to_json() + '\n')
        return result

    monkeypatch.setattr(loop_support, '_budgeted_formal_memories', budget)
    touches = _touches(monkeypatch, host.memory)
    request = RuntimeContextRequest('Find Alpha baseline。Find Beta supplement。', None, False,
                                    request_id='new-fixed-source', run_id='run')
    result = loop_support._formal_memories_for_request(host, request, routed,
        recall_scope=scope, long_term_memories=rows[:1], skip_formal_recall=False)
    expected = rows[:1] if remove_extra else rows
    assert len(budgets) == 4 and fixed
    assert [row.entry_id for row in result] == [row.entry_id for row in expected]
    _assert_access(host.memory, touches, expected)
    assert routed.supplement_entry_ids == ([] if remove_extra else [rows[1].entry_id])
