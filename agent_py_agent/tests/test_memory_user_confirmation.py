"""自动晋升守宿主来源、权威、证据与快照CAS；不合格候选仍可走精确人工审核。"""
import json
from dataclasses import replace

import pytest

from agent_py_agent.agent.capability.memory_tool import RememberTool
from agent_py_agent.agent.conversation.learning_source import (
    dispatch_projection,
    user_dialogue_projection,
)
from agent_py_agent.agent.memory_store.candidate_models import CandidateObservation, MemoryScope
from agent_py_agent.agent.memory_store.promotion import _automatic_policy_block
from agent_py_agent.tests.test_memory_promotion_v2 import _explicit, _lesson, _runtime
from agent_py_agent.tests.test_remember_learning_source_review import _agent


@pytest.mark.parametrize('model_confirmed', [False, True])
def test_host_verified_dialogue_autonomously_archives_without_model_authority(tmp_path, model_confirmed):
    agent = _agent(tmp_path, user_dialogue_projection(), {}, {})
    result = RememberTool(agent).execute(dict(content='项目使用 UTC 保存时间', kind='project',
        origin='user_explicit', subject_key='confirmation.rule', confirmed=model_confirmed, source_quote='用户已批准'))
    payload = json.loads(result.output)
    assert result.ok and payload['active_memory_changed'] and payload['saved']
    assert payload['results'][0]['promoted'] is True
    assert payload['results'][0]['status'] == 'promoted'
    [candidate] = agent.memory_candidates.list()
    assert candidate.status == 'promoted'
    assert [row.content for row in agent.memory.all()] == [candidate.content]
    assert candidate.learning_source['can_learn'] is True
    [approval] = [event for event in candidate.status_history if event['status'] == 'approved']
    assert approval['note'] == 'owner_autonomous_v1：结构化权威与安全门通过。'


def test_explicit_candidate_review_preserves_formal_commit_path(tmp_path):
    agent = _agent(tmp_path, dispatch_projection(), {}, {})
    draft = RememberTool(agent).execute(dict(content='项目使用 UTC 保存时间', kind='project',
        origin='user_explicit', subject_key='confirmation.rule', confirmed=True))
    payload = json.loads(draft.output)
    assert draft.ok and not payload['saved'] and not payload['active_memory_changed']
    assert payload['results'][0]['reason_code'] == 'AUTO_POLICY_LEARNING_SOURCE_UNVERIFIED'
    [candidate] = agent.memory_candidates.list()
    assert candidate.status == 'pending_review' and agent.memory.all() == []
    approved = agent.memory_promotion.review(candidate, approved=True, reviewer='explicit-user')
    result = agent.memory_promotion.promote(candidate.candidate_id, automatic=False,
        confirmed=True, expected_candidate=approved)
    assert result.promoted and result.formal_memory_changed
    [entry] = agent.memory.all()
    assert entry.content == candidate.content
    assert agent.memory_candidates.get(candidate.candidate_id).status == 'promoted'


def test_approved_candidate_can_auto_commit_without_rewriting_human_review(tmp_path):
    service, conversations, thread = _runtime(tmp_path)
    candidate = _explicit(service, conversations, thread, content='项目使用 UTC 保存时间')
    approved = service.review(candidate, approved=True, reviewer='explicit-user', note='精确人工审核')
    result = service.promote(candidate.candidate_id, automatic=True)
    assert result.promoted and result.formal_memory_changed
    saved = service.candidates.get(candidate.candidate_id)
    assert [event for event in saved.status_history if event['status'] == 'approved'] == [approved.status_history[-1]]
    replay = service.promote(candidate.candidate_id, automatic=True)
    assert replay.promoted and not replay.formal_memory_changed
    assert replay.reason_code == 'ALREADY_PROMOTED' and len(service.long_term.all()) == 1


@pytest.mark.parametrize('reviewed', [False, True])
def test_automatic_policy_never_revives_rejection_or_approves_manual_call(tmp_path, reviewed):
    service, conversations, thread = _runtime(tmp_path)
    candidate = _explicit(service, conversations, thread, content='项目使用 UTC 保存时间')
    if reviewed:
        service.review(candidate, approved=False, reviewer='explicit-user')
    result = service.promote(candidate.candidate_id, automatic=reviewed, confirmed=True)
    assert not result.promoted and result.reason_code == 'REVIEW_REQUIRED'
    assert service.candidates.get(candidate.candidate_id).status == ('rejected' if reviewed else 'pending_review')
    assert service.long_term.all() == []


@pytest.mark.parametrize('stage', ['approval', 'commit'])
def test_autonomous_approval_and_commit_both_require_complete_snapshot_cas(tmp_path, monkeypatch, stage):
    service, conversations, thread = _runtime(tmp_path)
    candidate = _explicit(service, conversations, thread, content='项目使用 UTC 保存时间')
    transition, commit = service.candidates.transition, service._commit_with_conflict_handling

    # LLM: 在两个真实CAS边界交错另一份审核，不替换验证器、候选仓库或正式写入。
    # 函数用途: 复现核验结束后候选被另一位审核者拒绝的情形。
    def concurrent_approval(expected, status, **kwargs):
        if status == 'approved':
            transition(candidate, 'rejected', reviewer='concurrent-user')
        return transition(expected, status, **kwargs)

    # LLM: 先真实改变已批准候选，再执行原提交锁快照检查，不伪造失败结果。
    # 函数用途: 复现自动审核完成到正式提交之间的拒绝竞争。
    def concurrent_commit(expected, *args, **kwargs):
        transition(expected, 'rejected', reviewer='concurrent-user')
        return commit(expected, *args, **kwargs)

    if stage == 'approval':
        monkeypatch.setattr(service.candidates, 'transition', concurrent_approval)
    else:
        monkeypatch.setattr(service, '_commit_with_conflict_handling', concurrent_commit)
    result = service.promote(candidate.candidate_id, automatic=True)
    assert not result.promoted and result.reason_code == 'CANDIDATE_CHANGED_AFTER_VERIFICATION'
    assert service.candidates.get(candidate.candidate_id).status == 'rejected'
    assert service.long_term.all() == []


@pytest.mark.parametrize('fields,code', [
    ({'promotion_target': 'agents'}, 'AUTO_AGENTS_WRITE_FORBIDDEN'),
    ({'learning_source': dispatch_projection()}, 'AUTO_POLICY_LEARNING_SOURCE_UNVERIFIED'),
    ({'origin': 'model_inferred'}, 'AUTO_POLICY_AUTHORITY_REQUIRES_REVIEW'),
    ({'proposed_action': 'replace'}, 'AUTO_POLICY_EXACT_TARGET_REQUIRED'),
    ({'proposed_action': 'remove'}, 'AUTO_POLICY_EXACT_TARGET_REQUIRED'),
    ({'proposed_action': 'merge'}, 'AUTO_POLICY_EXACT_TARGET_REQUIRED'),
    ({'conflicts_with': ['another-entry']}, 'AUTO_POLICY_CONFLICT_UNRESOLVED'),
    ({'scope': MemoryScope('temporary', 'temporary:review')}, 'AUTO_POLICY_TEMPORARY_REQUIRES_EXPIRY'),
])
def test_autonomous_policy_keeps_all_structured_blocks(tmp_path, fields, code):
    service, conversations, thread = _runtime(tmp_path)
    candidate = _explicit(service, conversations, thread, content='项目使用 UTC 保存时间')
    assert _automatic_policy_block(replace(candidate, **fields)) == code
    assert service.candidates.get(candidate.candidate_id) == candidate
    assert service.long_term.all() == []


@pytest.mark.parametrize('target,kind,code', [
    ('agents', 'working_agreement', 'AUTO_AGENTS_WRITE_FORBIDDEN'),
    ('soul', 'soul_change', 'PROMOTION_MANUAL_REQUIRED'),
])
def test_automatic_promotion_cannot_write_agents_or_soul(tmp_path, target, kind, code):
    service, conversations, thread = _runtime(tmp_path)
    candidate = _explicit(service, conversations, thread, content='修改人格必须经过原授权入口',
        promotion_target=target, candidate_type=kind)
    paths = [service.persona.owner_home / name for name in ('AGENTS.md', 'SOUL.md', 'USER.md')]
    before = [path.read_bytes() for path in paths]
    result = service.promote(candidate.candidate_id, automatic=True, confirmed=True)
    assert not result.promoted and result.reason_code == code
    assert service.candidates.get(candidate.candidate_id).status == 'pending_review'
    assert [path.read_bytes() for path in paths] == before


def test_user_preference_autonomously_archives_in_original_persona_repository(tmp_path):
    service, conversations, thread = _runtime(tmp_path)
    candidate = _explicit(service, conversations, thread, content='回答优先使用中文。',
        promotion_target='user', candidate_type='user_preference')
    result = service.promote(candidate.candidate_id, automatic=True)
    assert result.promoted and result.formal_memory_changed
    assert candidate.content in service.persona.load('user').content
    assert service.candidates.get(candidate.candidate_id).status == 'promoted'
    assert service.long_term.all() == []


def test_autonomous_lesson_and_hot_keep_original_thresholds_and_exact_lesson_link(tmp_path):
    service, _, _ = _runtime(tmp_path)
    candidate = _lesson(service, content='修改共享状态前先检查版本。', runs=('run-1', 'run-2'))
    lesson = service.promote(candidate.candidate_id, automatic=True)
    assert lesson.promoted and lesson.formal_memory_changed
    lesson_id = lesson.promotion_ref.rsplit('#', 1)[1]
    observation = CandidateObservation(candidate_type='hot_rule', content='共享状态必须先校验版本。',
        subject_key='hot.version', scope=MemoryScope('personal', 'personal'), origin='model_inferred',
        promotion_target='hot', target_entry_id=lesson_id, learning_source=user_dialogue_projection())
    for run in ('run-1', 'run-2', 'run-3'):
        hot = service.candidates.observe(replace(observation, source_run_ids=(run,), observation_id=run))
    result = service.promote(hot.candidate_id, automatic=True)
    assert result.promoted and result.formal_memory_changed
    assert lesson_id in service.hot.path.read_text(encoding='utf-8')
    assert len(service.lessons.list()) == 1 and service.long_term.all() == []
