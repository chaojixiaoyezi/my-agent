"""独立审阅反例：只调用被审组件，全部数据在 pytest 隔离目录。断言写要求，不迁就现状。"""
import hashlib
from dataclasses import replace
from types import SimpleNamespace as NS

import pytest

from agent_py_agent.agent.agent_core.orchestration.create_policy import create_task_attributes
from agent_py_agent.agent.agent_core.runtime.loop_support import _budgeted_formal_memories
from agent_py_agent.agent.capability.persona_repository import PersonaRepository
from agent_py_agent.agent.conversation import ConversationStore
from agent_py_agent.agent.conversation import learning_source as ls
from agent_py_agent.agent.conversation.runtime import (
    BackgroundRunRequest,
    _background_task_attributes,
)
from agent_py_agent.agent.gateway_parts.request_context import GatewayConversationContext
from agent_py_agent.agent.gateway_parts.request_execution import _gateway_task_attributes
from agent_py_agent.agent.memory_store.candidate_models import CandidateObservation, MemoryScope
from agent_py_agent.agent.memory_store.candidates import CandidateService
from agent_py_agent.agent.memory_store.curator import (
    _attach_learning_source,
    _fill_candidate_promotion_authority,
)
from agent_py_agent.agent.memory_store.jsonl import JsonlMemory, MemoryRecord
from agent_py_agent.agent.memory_store.lessons import HotRuleRepository, LessonRepository
from agent_py_agent.agent.memory_store.promotion import (
    ConversationMessageEvidenceVerifier,
    MemoryPromotionDependencies,
    MemoryPromotionService,
)
from agent_py_agent.tests.memory_confirmation_fixtures import confirm_candidate


@pytest.fixture
def env(tmp_path):
    owner = tmp_path / 'owner'
    owner.mkdir()
    for f in ('USER.md', 'SOUL.md', 'AGENTS.md'):
        (owner / f).write_text('# fixture\n')
    store = ConversationStore(tmp_path / 'conversations')
    service = MemoryPromotionService(dependencies=MemoryPromotionDependencies(
        candidates=CandidateService(owner / 'memory/candidates.jsonl'),
        long_term=JsonlMemory(owner / 'memory/long_term/memory.jsonl', ops_path=owner / 'memory/ops.jsonl'),
        persona=PersonaRepository(owner_home=owner, soul_path=owner / 'SOUL.md', user_path=owner / 'USER.md', agents_path=owner / 'AGENTS.md'),
        lessons=LessonRepository(owner / 'memory/lessons', owner / 'memory/routing/INDEX.md'),
        hot=HotRuleRepository(owner / 'memory-hot.md'),
        message_verifier=ConversationMessageEvidenceVerifier(store),
        exclusion_store=store,
    ))
    return NS(owner=owner, store=store, service=service)


def thread(env, source, name='conversation'):
    return env.store.threads.get_or_create(dict(canonical_user_id='local/test', channel='chat', channel_user_id='local/test', channel_conversation_id=name, learning_source=source))


# LLM: 夹具从真实隔离仓库读取目标版本，模拟宿主观察快照；不改变原安全断言。
# 函数用途: 构造带来源及真实目标版本的宿主候选观察。
def observation(env, t, text, *, action='add', target='', options=None):
    options = options or {}
    ctype = options.get('ctype', 'working_agreement')
    origin = options.get('origin', 'model_inferred')
    destination = options.get('destination', 'long_term')
    msg = env.store.messages.append(dict(thread_id=t.thread_id, role='user', content=text))
    ref = dict(message_id=msg.message_id, thread_id=t.thread_id, role='user', content_hash='sha256:' + hashlib.sha256(text.encode()).hexdigest(), quote=text)
    obs = CandidateObservation(candidate_type=ctype, content=text, subject_key='review38.workflow', scope=MemoryScope('personal', 'personal'), origin=origin, source_message_refs=(ref,), proposed_action=action, target_entry_id=target, promotion_target=destination)
    batch = NS(messages=(NS(message_id=msg.message_id, thread_id=t.thread_id, metadata=msg.metadata, thread_source=t.learning_source),))
    version = next((row.version for row in env.service.long_term.all() if row.entry_id == target), None)
    obs = replace(obs, target_entry_version=version)
    return _fill_candidate_promotion_authority(_attach_learning_source(obs, batch, 'local/test'))


def submit(env, obs):
    candidate = env.service.candidates.observe(obs)
    return confirm_candidate(env.service, candidate.candidate_id)


def run_agent(attrs):
    return NS(_current_run_params=NS(task_id='', task_attributes=attrs), home_paths=NS(owner_home_dir='', owner_id='local/test'))


def test_gateway_dispatch_thread_cannot_grant_child_learning(env):
    t = thread(env, ls.dispatch_projection())
    context = GatewayConversationContext(thread_id=t.thread_id)
    attrs = _gateway_task_attributes(context)
    child_attrs = create_task_attributes({'goal': 'child'}, run_agent(attrs))
    source = ls.child_projection_from_parent(child_attrs.get('learning_source'), run_id='child', task_id='root')
    assert not ls.projection_is_learnable(source), source


@pytest.mark.parametrize('reason,metadata', [
    ('scheduled_job_due', {'scheduler_run_id':'schedule-run', 'scheduler_job_id':'job'}),
    ('session_task', {'session_task_id':'session-task'}),
    ('thread_goal_continue', {}),
])
def test_real_background_attributes_cannot_grant_child_learning(reason, metadata):
    req = BackgroundRunRequest(thread_id='thread', reason=reason, task_id='task', wake_signal={'wake_signal_id':'wake', 'metadata':metadata})
    attrs = _background_task_attributes('thread', req, None)
    child_attrs = create_task_attributes({'goal':'child'}, run_agent(attrs))
    child = ls.child_projection_from_parent(child_attrs.get('learning_source'), run_id='child')
    assert not ls.projection_is_learnable(child), child


@pytest.mark.parametrize('ctype,destination', [('long_term_fact','long_term'), ('user_preference','user')])
def test_dispatch_user_role_cannot_auto_promote_other_candidate_types(env, ctype, destination):
    t = thread(env, ls.dispatch_projection())
    obs = observation(env, t, '每次任务必须先读取任务全文。', options={'ctype': ctype, 'origin': 'user_explicit', 'destination': destination})
    assert obs.learning_source['can_learn'] is False
    result = submit(env, obs)
    assert not result.promoted, result
    assert env.service.long_term.all() == []
    assert (env.owner / 'USER.md').read_text() == '# fixture\n'


def test_pending_merge_keeps_observed_version_after_newer_write(env):
    t = thread(env, ls.user_dialogue_projection())
    assert submit(env, observation(env, t, '约定：先读全文。')).promoted
    before = env.service.long_term.all()[0]
    obs = observation(env, t, '约定：先读全文，之后提交旧方案。', action='merge', target=before.entry_id)
    pending = env.service.candidates.observe(obs)
    env.service.long_term.apply_batch([dict(action='replace', entry_id=before.entry_id, content='约定：用户已撤销旧方案，改用新方案。', expected_version=before.version)])
    current = env.service.long_term.all()[0]
    assert current.version > before.version
    result = confirm_candidate(env.service, pending.candidate_id)
    after = env.service.long_term.all()[0]
    assert not result.promoted and after.content == current.content, (result, before.version, current.version, after.version, after.content)


@pytest.mark.parametrize('first_source', ['user', 'subagent'])
def test_multiple_learnable_refs_preserve_all_child_lineage(env, first_source):
    parent = ls.user_dialogue_projection() if first_source == 'user' else ls.subagent_projection(trigger_kind='user_dialogue', run_ids=['child-a'], task_ids=['task-a'])
    t1 = thread(env, parent, 'one')
    t2 = thread(env, ls.subagent_projection(trigger_kind='user_dialogue', run_ids=['child-b'], task_ids=['task-b']), 'two')
    o1 = observation(env, t1, '约定：先读全文。')
    o2 = observation(env, t2, '约定：先读全文。')
    refs = (*o1.source_message_refs, *o2.source_message_refs)
    messages = tuple(NS(message_id=ref['message_id'], thread_id=t.thread_id, metadata={}, thread_source=t.learning_source) for ref, t in zip(refs, (t1,t2)))
    obs = _attach_learning_source(replace(o1, source_message_refs=refs), NS(messages=messages), 'local/test')
    assert submit(env, obs).promoted
    source = env.service.long_term.all()[0].attributes['learning_source']
    assert source['subagent_tag'] == 'subagent' and 'child-b' in source['source_run_ids'], source
    if first_source == 'subagent':
        assert set(source['source_run_ids']) == {'child-a','child-b'}, source


def test_candidate_merge_must_recheck_new_unlearnable_refs(env):
    good = observation(env, thread(env, ls.user_dialogue_projection(), 'user'), '约定：先读全文。')
    bad = observation(env, thread(env, ls.dispatch_projection(), 'dispatch'), '约定：先读全文。')
    first = env.service.candidates.observe(good)
    second = env.service.candidates.observe(bad)
    assert first.candidate_id == second.candidate_id
    assert len(second.source_message_refs) == 2
    result = confirm_candidate(env.service, second.candidate_id)
    assert not result.promoted, (result, second.learning_source)


@pytest.mark.parametrize('size', [4001, 10001])
def test_oversized_candidate_is_rejected_before_budgeting(env, size):
    t = thread(env, ls.user_dialogue_projection())
    with pytest.raises(ValueError, match='1..2000 characters'):
        submit(env, observation(env, t, '约' * size))
    # 初版探针假设自动候选可写超过4000字，但真实入口已有2000字上限。
    # 因前提不成立，不把first-item预算假设报成自动学习链漏洞。
    assert env.service.long_term.all() == []


def _exclude(env, chat):
    import json
    entries = [{'channel_conversation_id': 'synthetic-' + str(i)} for i in range(40)]
    entries.append({'channel_conversation_id': chat})
    (env.owner / 'memory_policy.json').write_text(json.dumps({'learning_exclusions': entries}))


def test_exclusion_list_overrides_learnable_projection(env):
    t = thread(env, ls.user_dialogue_projection(), 'excluded')
    obs = observation(env, t, '约定：只用于当前派活。')
    _exclude(env, 'excluded')
    result = submit(env, obs)
    assert not result.promoted and result.reason_code == 'LEARNING_SOURCE_EXCLUDED', result
    assert env.service.long_term.all() == []


def test_excluded_gateway_parent_must_not_learn_through_child(env):
    parent = thread(env, ls.dispatch_projection(), 'excluded')
    _exclude(env, 'excluded')
    attrs = _gateway_task_attributes(GatewayConversationContext(thread_id=parent.thread_id))
    frozen = create_task_attributes({'goal':'child'}, run_agent(attrs))
    child = ls.child_projection_from_parent(frozen.get('learning_source'), run_id='run-child')
    ct = env.store.threads.ensure_agent(dict(thread_id='thread-child', agent_run_id='run-child', canonical_user_id='local/test', owner_id='local/test', parent_thread_id=parent.thread_id, learning_source=child))
    result = submit(env, observation(env, ct, '约定：来源为派活的子代理总结。'))
    assert not result.promoted, result


def test_deleted_agreement_cannot_revive_from_stale_indexes(env, monkeypatch):
    from agent_py_agent.agent.agent_core.runtime.loop_support import _refresh_recall_candidates
    from agent_py_agent.agent.memory_store.recall import (
        MemoryRecallScope,
        long_term_record_matches_scope,
    )
    t = thread(env, ls.user_dialogue_projection())
    assert submit(env, observation(env, t, '约定：提交之前必须核对。')).promoted
    stale = env.service.long_term.all()[0]
    env.service.long_term.remove(stale.entry_id, expected_version=stale.version)
    monkeypatch.setattr(env.service.long_term, '_search_local_store_report', lambda *args: ([stale], []))
    monkeypatch.setattr(env.service.long_term, '_semantic_records', lambda *args: [stale])
    scope = MemoryRecallScope.from_runtime()
    assert env.service.long_term.search_scoped('提交核对', 5, lambda row: long_term_record_matches_scope(row, scope)) == []
    agent = NS(memory=env.service.long_term, memory_hot=env.service.hot, memory_lessons=env.service.lessons, config=NS(home_lesson_stale_caveat_days=7))
    assert _refresh_recall_candidates(agent, [stale], [], scope) == []


def test_concurrent_merge_intents_must_not_silently_overwrite_each_other(env):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    t = thread(env, ls.user_dialogue_projection())
    assert submit(env, observation(env, t, '约定：先读全文。')).promoted
    prior = env.service.long_term.all()[0]
    candidates = [env.service.candidates.observe(observation(env, t, text, action='merge', target=prior.entry_id)) for text in ('约定：先读全文并保留甲信息。', '约定：先读全文并保留乙信息。')]
    start = Barrier(2)
    def promote(candidate):
        start.wait(timeout=10)
        return confirm_candidate(env.service, candidate.candidate_id)
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(promote, candidates))
    # 两个提案都是v1上的合并：可以拒绝陈旧提案，或者重新合并后保留二者；不能两次成功但丢一方。
    current = env.service.long_term.all()[0]
    assert sum(result.promoted for result in results) == 1 or ('甲信息' in current.content and '乙信息' in current.content), (results, current.content, current.version)


def test_tui_direct_entry_must_supply_user_learning_facts():
    from agent_py_agent.cli.chat_parts.tui_worker_local import _worker_local_path
    captured = {}
    class StopBeforeModel(Exception):
        pass
    def run(prompt, **kwargs):
        captured.update(kwargs)
        raise StopBeforeModel()
    job = NS(system_task=None, input_media=[], request_id='request-tui', user='普通用户对话', prompt_files=[], save=True, resume_context=False)
    control = NS(request_id='request-tui', check_admission=lambda: None)
    ctx = NS(job=job, cfg=NS(agent=NS(run=run), local_run_ref=[control]), turn_inject=[], turn_adapter=None)
    with pytest.raises(StopBeforeModel):
        _worker_local_path(ctx)
    source = ls.spawn_source_projection(captured['task_attributes'])
    assert ls.projection_is_learnable(source), captured['task_attributes']


@pytest.mark.parametrize('existing', [False, True])
def test_gateway_current_user_message_has_learnable_facts(env, monkeypatch, existing):
    from agent_py_agent.agent.gateway_parts import request_context as rc
    from agent_py_agent.agent.gateway_parts import request_history as rh
    spec = dict(canonical_user_id='local/test', channel='chat', channel_user_id='local/test', channel_conversation_id='gateway')
    if existing:
        env.store.threads.get_or_create(spec)  # 旧版本真实存储形状，尚无learning_source。
    agent = NS(conversation_store=env.store, home_paths=NS(owner_id='local/test', owner_home_dir=env.owner))
    # 仅隔离workspace验证和索引副作用，不替换来源、消息或候选核心逻辑。
    monkeypatch.setattr(rc, 'gateway_request_workspace_scope', lambda *args: (str(env.owner), (str(env.owner),)))
    monkeypatch.setattr(rh, 'index_conversation_message', lambda *args: None)
    request = {'conversation':spec}
    t, error = rc._load_gateway_thread(rc.GatewayConversationLoadRequest(agent=agent, request=request, request_id='request-new', prompt='普通用户新对话'), env.store, spec)
    assert error is None
    context = rc.GatewayConversationContext(thread_id=t.thread_id)
    assert rh.append_gateway_conversation_message(agent, request, context, request_id='request-new', role='user', content='普通用户新对话')
    entries, errors = env.store.messages.recent_report(t.thread_id, limit=0)
    assert not errors
    msg = entries[-1]
    facts = ls.facts_for_message(msg.metadata, owner='local/test', thread_id=t.thread_id, thread_source=t.learning_source)
    assert facts.can_learn, facts
