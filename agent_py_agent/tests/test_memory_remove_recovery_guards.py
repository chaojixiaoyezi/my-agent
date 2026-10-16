"""删除恢复的负向边界：无新写入、零部分批次、旧计划不串目标、派生失败可重试。"""
import json
from dataclasses import replace

import pytest

from agent_py_agent.agent.memory_store.promotion_remove_recovery import (
    prepare_committed_remove,
    recover_prepared_remove,
)
from agent_py_agent.agent.retrieval.vector_store import VectorStore
from agent_py_agent.tests.memory_confirmation_fixtures import ConfirmedRememberWorkflow
from agent_py_agent.tests.test_memory_remove_entry_recovery import _crashed_agent


# LLM: 真实入口重试不得调用新晋升入口；证明清理与自动学习开关解耦。
# 函数用途: 自动晋升不可用时仍只清理已经提交的删除。
def test_entry_recovery_never_calls_new_promotion(tmp_path, monkeypatch):
    agent, removed = _crashed_agent(tmp_path)
    def forbidden(*args, **kwargs):
        raise AssertionError('must not start a new promotion')
    monkeypatch.setattr(agent.memory_promotion, 'promote', forbidden)
    result = ConfirmedRememberWorkflow(agent).execute(dict(action='remove', origin='user_explicit', entry_id=removed.target_entry_id))
    assert result.ok and json.loads(result.output)['saved'], result
    assert all(not row.content for row in agent.memory_candidates.list())


# LLM: 后面的坏操作必须使前面恢复也零副作用；不能借“只清理”绕过全批验证。
# 函数用途: 验证混合恢复批次仍全量校验后才写入。
def test_invalid_batch_keeps_pending_recovery_untouched(tmp_path):
    agent, removed = _crashed_agent(tmp_path, indexed=True)
    before = agent.memory_candidates.path.read_bytes()
    result = ConfirmedRememberWorkflow(agent).execute(dict(action='batch', operations=[
        dict(action='remove', origin='user_explicit', entry_id=removed.target_entry_id),
        dict(action='replace', origin='user_explicit', entry_id='missing-target', content='新规则'),
    ]))
    assert not result.ok
    assert agent.memory_candidates.path.read_bytes() == before
    assert agent.memory.local_store.search('备份', source_type='memory')


# LLM: 精确条目和scope约束在恢复入口仍有效，拒绝不得触及候选历史。
# 函数用途: 错目标及越范围恢复请求不会借墓碑绕过校验。
@pytest.mark.parametrize('wrong', ['entry', 'scope', 'subject'])
def test_recovery_rejects_wrong_binding(tmp_path, wrong):
    agent, removed = _crashed_agent(tmp_path)
    operation = dict(action='remove', origin='user_explicit', entry_id=removed.target_entry_id)
    operation.update({'entry': {'entry_id': removed.target_entry_id + '-other'},
        'scope': {'scope': {'scope_type': 'global', 'scope_key': 'global'}},
        'subject': {'subject_key': 'different-subject'}}[wrong])
    before = agent.memory_candidates.path.read_bytes()
    result = ConfirmedRememberWorkflow(agent).execute(operation)
    assert not result.ok
    assert agent.memory_candidates.path.read_bytes() == before


# LLM: 索引失败不允许清候选终态；后续同一工具入口重试必须实际移除外置正文和搜索命中。
# 函数用途: 隐私清理失败保留责任，解除故障后可以恢复。
def test_projection_failure_remains_retryable_from_entry(tmp_path, monkeypatch):
    agent, removed = _crashed_agent(tmp_path, indexed=True)
    store = agent.memory.local_store
    [hit] = store.search('备份', source_type='memory')
    body = store._resolve_content_path(hit.content_path)
    assert body.read_text()
    delete = store.delete_record
    def failed_delete(*args):
        raise OSError('injected cleanup failure')
    monkeypatch.setattr(store, 'delete_record', failed_delete)
    operation = dict(action='remove', origin='user_explicit', entry_id=removed.target_entry_id)
    result = json.loads(ConfirmedRememberWorkflow(agent).execute(operation).output)
    assert not result['saved'] and not result['active_memory_changed']
    assert result['results'][0]['reason_code'] == 'REMOVE_CLEANUP_PENDING'
    assert agent.memory_candidates.get(removed.candidate_id).content
    monkeypatch.setattr(store, 'delete_record', delete)
    result = json.loads(ConfirmedRememberWorkflow(agent).execute(operation).output)
    assert result['saved'] and not result['active_memory_changed']
    assert not store.search('备份', source_type='memory')
    assert not body.exists() or not body.read_bytes()


# LLM: 停用embedding或旧候选已标终态都不能留下持久向量正文；坏文件必须保留待恢复失败。
# 函数用途: 清除禁用检索后的旧向量，并正确区分未知和确实已删。
@pytest.mark.parametrize('corrupt', [False, True])
def test_vector_cleanup_without_active_embedder(tmp_path, corrupt):
    agent, removed = _crashed_agent(tmp_path)
    assert agent.memory._embedder is None
    path = agent.memory._vector_store_path()
    VectorStore(path).upsert(removed.target_entry_id, [1., 0.], text='遗留秘密备份')
    if corrupt:
        path.write_text('{broken')
    result = agent.memory_promotion.promote(removed.candidate_id, automatic=True)
    assert result.promoted is (not corrupt), result
    if corrupt:
        assert result.reason_code == 'REMOVE_CLEANUP_PENDING'
        assert agent.memory_candidates.get(removed.candidate_id).content
    else:
        assert not VectorStore(path).search([1., 0.])
        assert '遗留秘密备份' not in path.read_text()


# LLM: 恢复计划快照被改后不得回退新晋升；不因subject相同或entry近似扩大清理范围。
# 函数用途: 校验恢复计划的候选版本CAS和精确墓碑来源。
def test_stale_recovery_plan_has_no_write_fallback(tmp_path):
    agent, removed = _crashed_agent(tmp_path)
    plan = prepare_committed_remove(agent.memory_promotion, removed.target_entry_id)
    assert plan is not None
    stale = replace(plan, candidate=replace(plan.candidate, target_entry_version=1))
    before = agent.memory_candidates.path.read_bytes()
    result = recover_prepared_remove(agent.memory_promotion, stale)
    assert not result.promoted and result.reason_code == 'CANDIDATE_CHANGED_AFTER_VERIFICATION'
    assert agent.memory_candidates.path.read_bytes() == before


# LLM: legacy无hash的关联清理只按精确target/ref，不因近似entry或主题将另一条候选一并删除。
# 函数用途: 补历史清理时保留无关记录的正文和引用。
def test_legacy_recovery_preserves_unrelated_candidate(tmp_path):
    from agent_py_agent.agent.memory_store.candidate_models import CandidateObservation
    agent, removed = _crashed_agent(tmp_path, legacy=True)
    unrelated = agent.memory_candidates.observe(CandidateObservation(
        candidate_type='project', content='无关项目独立保留', subject_key='source.review',
        scope=removed.scope, origin='model_inferred', proposed_action='replace',
        target_entry_id=removed.target_entry_id + '-different', target_entry_version=1))
    result = agent.memory_promotion.promote(removed.candidate_id, automatic=True)
    assert result.promoted, result
    assert agent.memory_candidates.get(unrelated.candidate_id) == unrelated
    assert all(not row.content for row in agent.memory_candidates.list() if row.candidate_id != unrelated.candidate_id)


# LLM: 旧版本可能先标记候选而未清派生层；终态和正文已空不等于没有清理责任。
# 函数用途: 已完成候选的删除重试仍清理历史遗留搜索正文。
def test_redacted_candidate_still_cleans_stale_index(tmp_path):
    agent, removed = _crashed_agent(tmp_path, indexed=True)
    assert agent.memory_promotion.promote(removed.candidate_id, automatic=True).promoted
    assert not agent.memory_candidates.get(removed.candidate_id).content
    store = agent.memory.local_store
    store.upsert_record(source_type='memory', source_id=removed.target_entry_id,
                        title='legacy stale record', content='遗留备份正文', metadata={})
    assert store.search('备份', source_type='memory')
    before = agent.memory.path.read_bytes()
    result = ConfirmedRememberWorkflow(agent).execute(dict(action='remove', origin='user_explicit', entry_id=removed.target_entry_id))
    assert result.ok and json.loads(result.output)['saved'], result
    assert not store.search('备份', source_type='memory')
    assert agent.memory.path.read_bytes() == before


# LLM: batch中的非对象操作也属于全批校验失败，不能过滤坏项后悄悄执行前面的恢复。
# 函数用途: 阻止混合批次静默丢弃无效元素再产生清理副作用。
@pytest.mark.parametrize('invalid', [None, '', 7])
def test_non_object_batch_item_prevents_recovery(tmp_path, invalid):
    agent, removed = _crashed_agent(tmp_path, indexed=True)
    before = agent.memory_candidates.path.read_bytes()
    result = ConfirmedRememberWorkflow(agent).execute(dict(action='batch', operations=[
        dict(action='remove', origin='user_explicit', entry_id=removed.target_entry_id), invalid,
    ]))
    assert not result.ok
    assert agent.memory_candidates.path.read_bytes() == before
    assert agent.memory.local_store.search('备份', source_type='memory')
