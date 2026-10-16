"""当前正式内容确认规则：来源核验不代替用户对精确候选的批准。"""
import json

from agent_py_agent.agent.capability.memory_tool import RememberTool
from agent_py_agent.agent.conversation.learning_source import user_dialogue_projection
from agent_py_agent.tests.memory_confirmation_fixtures import confirm_candidate
from agent_py_agent.tests.test_remember_learning_source_review import _agent


def test_host_verified_dialogue_and_model_confirmed_cannot_publish(tmp_path):
    agent = _agent(tmp_path, user_dialogue_projection(), {}, {})
    result = RememberTool(agent).execute(dict(content='项目使用 UTC 保存时间', kind='project',
        origin='user_explicit', subject_key='confirmation.rule', confirmed=True, source_quote='用户已批准'))
    payload = json.loads(result.output)
    assert result.ok and not payload['active_memory_changed'] and not payload['saved']
    assert payload['results'][0]['reason_code'] == 'AUTO_PROMOTION_CONFIRMATION_REQUIRED'
    assert '等待用户审核确认' in payload['hint']
    assert payload['required_repairs'][0]['action'] == 'user_review_required'
    assert payload['required_repairs'][0]['model_retry_authorizes'] is False
    [candidate] = agent.memory_candidates.list()
    assert candidate.status == 'pending_review'
    assert agent.memory.all() == []
    # 宿主已核实来源仍只赋候选资格，不能把模型字段变成批准。
    assert candidate.learning_source['can_learn'] is True


def test_explicit_candidate_review_preserves_formal_commit_path(tmp_path):
    agent = _agent(tmp_path, user_dialogue_projection(), {}, {})
    draft = RememberTool(agent).execute(dict(content='项目使用 UTC 保存时间', kind='project',
        origin='user_explicit', subject_key='confirmation.rule'))
    assert not json.loads(draft.output)['saved'] and agent.memory.all() == []
    [candidate] = agent.memory_candidates.list()
    approved = confirm_candidate(agent.memory_promotion, candidate.candidate_id)
    assert approved.promoted and approved.formal_memory_changed
    [entry] = agent.memory.all()
    assert entry.content == candidate.content
    assert agent.memory_candidates.get(candidate.candidate_id).status == 'promoted'


def test_approved_candidate_does_not_enable_automatic_publish(tmp_path):
    agent = _agent(tmp_path, user_dialogue_projection(), {}, {})
    RememberTool(agent).execute(dict(content='项目使用 UTC 保存时间', kind='project',
        origin='user_explicit', subject_key='confirmation.rule'))
    [candidate] = agent.memory_candidates.list()
    agent.memory_promotion.review(candidate.candidate_id, approved=True, reviewer='explicit-user')
    denied = agent.memory_promotion.promote(candidate.candidate_id, automatic=True, confirmed=True)
    assert not denied.promoted and denied.reason_code == 'AUTO_PROMOTION_CONFIRMATION_REQUIRED'
    assert agent.memory.all() == []
    assert agent.memory_promotion.promote(candidate.candidate_id, confirmed=True).promoted
