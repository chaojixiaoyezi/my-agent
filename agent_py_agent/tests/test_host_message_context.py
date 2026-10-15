"""原根输入的引用上下文：真实Store/RuntimeDB，仅合成通道与provider，无生产IO。"""
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.adapter.protocol import provider_message_reference
from agent_py_agent.agent.agent_core.runtime.loop_models import RunParams
from agent_py_agent.agent.conversation.host_actor import actor_from_scope, task_host_actor
from agent_py_agent.agent.conversation.host_message_context import (
    CONTEXT_KEY,
    HostMessageContext,
    references_for_run,
)


# LLM: 沿真实委派/Store/主根晋升创建原输入，仅替代模型路由和传输；不接生产平台。
# 函数用途: 建立可恢复的委派根及其原引用，供主子孙和阴性合同测试复用。
def delegated_case(tmp_path, monkeypatch, *, conversation_id='oc_a'):
    from agent_py_agent.agent.conversation.im_delegation import prepare_delegated_execution
    from agent_py_agent.tests.test_im_delegation_runtime import runtime_fixture
    from agent_py_agent.tests.test_schedule_control import _agent

    refs = ((provider_message_reference('om_original'), 'om_original'),)
    owned = _agent(tmp_path, group='oc_a')
    monkeypatch.setattr('agent_py_agent.tests.test_im_delegation_runtime.SimpleAgent', lambda *_args: owned)

    # LLM: 仅替代已独立覆盖的Gateway解析边界，真实SessionTask/TaskLink/RuntimeDB保持完整。
    # 函数用途: 为合成委派fixture提供同请求认证引用。
    def capture(context, conversation):
        actor = actor_from_scope(context.agent, SimpleNamespace(channel='feishu', channel_user_id='ou_a',
            channel_conversation_id=conversation_id), conversation.thread_id, context.request_id)
        return HostMessageContext(actor, refs)

    monkeypatch.setattr('agent_py_agent.agent.gateway_parts.im_task_routing._message_context', capture)
    agent, params, task, context, source = runtime_fixture(tmp_path, monkeypatch, conversation_id=conversation_id)
    actor = agent._current_run_params.host_channel_actor
    from agent_py_agent.tests.test_group_authorization import (
        grant_synthetic_group_monitor_capability,
    )

    grant_synthetic_group_monitor_capability(agent, actor, members={'ou_a'})
    prepare_delegated_execution(agent, params)
    return agent, actor, refs, task, context, source


# LLM: attempt只经原公开create_attempt激活，不伪造数据库行；当前子身份必须与实际存档相符。
# 函数用途: 将合成子/孙切换为当前执行者，不启动进程。
def select_child(agent, child):
    from agent_py_agent.agent.agent_core.subagent.run_flow import _subagent_model_run_params

    repo = agent.subagents.runtime_db
    record = repo.agent_run_for_run_id(child.id)
    attempt = repo.create_attempt(record['agent_run_id'], reuse_pending=True)
    agent._current_subagent_run_id = child.id
    iteration = SimpleNamespace(current=SimpleNamespace(injection='', history_seed=[], compact_context=None),
        carried_archive_tool_calls=[], carried_active_turn_user_inputs=[], runtime_rejected_actions=[], transcript_sink=None)
    agent._current_run_params = _subagent_model_run_params(context=agent.subagents.runner_context.build_execution_context(child.id),
        prompt=child.goal, attempt_id=attempt['attempt_id'], task_attributes=child.attributes,
        iteration=iteration, host_channel_actor=task_host_actor(agent, child))
    return record


@pytest.mark.parametrize('topic', [False, True])
def test_delegate_root_child_grandchild_and_restore_use_single_original_input(tmp_path, monkeypatch, topic):
    from agent_py_agent.agent.conversation.store import ConversationStore
    from agent_py_agent.agent.subagents.services.hierarchy.scheduler import (
        HierarchyChildSpec,
        HierarchyScheduleRequest,
    )

    agent, actor, refs, task, context, _ = delegated_case(tmp_path, monkeypatch,
        conversation_id='oc_a:thread:om_topic' if topic else 'oc_a')
    link = agent.conversation_store.tasks.load(task.task_id)
    assert link.host_message_context == {'source': 'session_task', 'task_id': task.task_id}
    assert references_for_run(agent, actor) == refs
    child = agent.subagents.list_runs()[0]
    assert CONTEXT_KEY not in child.attributes and not agent._current_run_params.host_message_references
    select_child(agent, child)
    assert references_for_run(agent, actor) == refs
    result = agent.subagents.hierarchy.schedule_child_runs(params=HierarchyScheduleRequest(parent_run_id=child.id,
        child_specs=[HierarchyChildSpec(goal='合成孙', role='worker')], apply=True, requested_by=child.id))
    grandchild = agent.subagents.load(result.created_run_ids[0])
    select_child(agent, grandchild)
    assert references_for_run(agent, actor) == refs
    # 原Gateway文件已GC仍只从持久根输入恢复；重开同一Store不是新引用账本。
    context.request_path.unlink()
    agent.conversation_store = ConversationStore(agent.conversation_store.storage.root)
    assert references_for_run(agent, actor) == refs


@pytest.mark.parametrize('corruption', ['new_gateway', 'stale_attempt', 'wrong_owner', 'wrong_root', 'forged_ref', 'wrong_actor'])
@pytest.mark.parametrize('topic', [False, True])
def test_original_context_rejects_unrelated_or_stale_execution(tmp_path, monkeypatch, corruption, topic):
    from dataclasses import replace

    agent, actor, refs, task, _, _ = delegated_case(tmp_path, monkeypatch,
        conversation_id='oc_a:thread:future_topic' if topic else 'oc_a')
    child = agent.subagents.list_runs()[0]
    record = select_child(agent, child)
    current = agent._current_run_params
    if corruption == 'new_gateway':
        current.source, current.request_id = 'gateway', 'new-independent-input'
        current.host_message_references = refs
    elif corruption == 'stale_attempt':
        agent.subagents.runtime_db.create_attempt(record['agent_run_id'])
    elif corruption == 'wrong_owner':
        actor = replace(actor, owner_id='other-owner')
    elif corruption == 'wrong_root':
        current.task_id = 'another-root'
    elif corruption == 'forged_ref':
        # 模型属性与当前子字段不能覆盖原根精确快照。
        current.host_message_references = ((refs[0][0], 'om_forged'),)
        current.task_attributes[CONTEXT_KEY] = {'references': [{'ref': refs[0][0], 'message_id': 'om_forged'}]}
        assert references_for_run(agent, actor) == refs
        return
    else:
        actor = replace(actor, actor_id='ou_other')
    with pytest.raises((ValueError, RuntimeError)):
        references_for_run(agent, actor)


def test_delegated_replay_cannot_replace_original_reference_input(tmp_path, monkeypatch):
    from agent_py_agent.agent.gateway_parts.im_task_routing import prepare_im_task_dispatch
    from agent_py_agent.agent.runtime_errors import DataCorruptionError

    agent, actor, _, task, context, source = delegated_case(tmp_path, monkeypatch)
    monkeypatch.setattr('agent_py_agent.agent.gateway_parts.im_task_routing._message_context',
        lambda *_: HostMessageContext(actor, ((provider_message_reference('om_new'), 'om_new'),)))
    before = agent.conversation_store.session_tasks.load(task.task_id).to_dict()
    with pytest.raises(DataCorruptionError):
        prepare_im_task_dispatch(context, source, task.goal)
    assert agent.conversation_store.session_tasks.load(task.task_id).to_dict() == before


@pytest.mark.parametrize('topic', [False, True])
def test_delegated_child_creates_nested_subscription_via_original_proof_path(tmp_path, monkeypatch, topic):
    import json

    from agent_py_agent.agent.scheduler import event_source_tool as sources
    from agent_py_agent.agent.scheduler.tool import ScheduleTool

    agent, _, refs, _, _, _ = delegated_case(tmp_path, monkeypatch,
        conversation_id='oc_a:thread:om_topic' if topic else 'oc_a')
    select_child(agent, agent.subagents.list_runs()[0])
    seen = []
    monkeypatch.setattr(sources, 'group_authority_snapshot', lambda *_: {'provider': 'feishu', 'chat_id': 'oc_a',
        'app_id': 'cli_receiver', 'tenant_key': 'tenant', 'bot_is_member': True, 'members_complete': True})

    # LLM: 精确Get替身只记录真实om_编号；完整工具链仍执行原授权、归一化及持久化。
    # 函数用途: 验证公开引用不会作为供应商ID发出，并返回合成同群app证明。
    def get_source(_agent, chat, message):
        seen.append((chat, message))
        return {'app_id': 'cli_receiver', 'chat_id': chat, 'source_message_id': message,
            'sender_id': 'cli_other_bot', 'sender_id_type': 'app_id'}

    monkeypatch.setattr(sources, 'resolve_bot_source', get_source)
    result = ScheduleTool(agent).execute({'action': 'create', 'name': '合成订阅', 'rules': [{
        'id': 'one', 'name': '合成规则', 'use_llm': True, 'prompt': '合成一行',
        'schedule': {'kind': 'event_source', 'source_message_refs': [refs[0][0]], 'min_interval_seconds': 60}}]})
    assert result.ok, (result.error_code, result.output)
    assert seen == [('oc_a', 'om_original')]
    proof = json.loads(result.output)['job']['rules'][0]['schedule']['source']['proofs']
    assert proof == [{'sender_id': 'cli_other_bot', 'source_message_id': 'om_original'}]


@pytest.mark.parametrize('generation', [1, 2])
@pytest.mark.parametrize('fault', ['', 'root', 'parent', 'self_parent'])
def test_business_main_role_cannot_skip_original_child_lineage(tmp_path, monkeypatch, generation, fault):
    from agent_py_agent.agent.subagents.services.hierarchy.scheduler import (
        HierarchyChildSpec,
        HierarchyScheduleRequest,
    )

    agent, actor, refs, _, _, _ = delegated_case(tmp_path, monkeypatch)
    parent = agent.subagents.list_runs()[0]
    select_child(agent, parent)
    for _ in range(generation):
        result = agent.subagents.hierarchy.schedule_child_runs(params=HierarchyScheduleRequest(parent_run_id=parent.id,
            child_specs=[HierarchyChildSpec(goal='合成main角色', role='main')], apply=True, requested_by=parent.id))
        parent = agent.subagents.load(result.created_run_ids[0])
        select_child(agent, parent)
    assert parent.role == 'main'
    if not fault:
        assert references_for_run(agent, actor) == refs
        return
    if fault == 'root':
        parent.root_id = 'forged-root'
    elif fault == 'parent':
        parent.parent_id = 'missing-parent'
    else:
        # 同owner/actor/root的真实记录也不能冒充RuntimeDB冻结的父。
        parent.parent_id = parent.id
    agent.subagents.save(parent)
    with pytest.raises(ValueError):
        references_for_run(agent, actor)


def test_nonempty_runtime_parent_missing_row_is_not_first_root_child(tmp_path, monkeypatch):
    agent, actor, _, task, _, _ = delegated_case(tmp_path, monkeypatch)
    child = agent.subagents.list_runs()[0]
    select_child(agent, child)
    assert child.parent_id == task.task_id
    repo = agent.subagents.runtime_db
    original_get = repo.get_agent_run

    # LLM: 只模拟数据库损坏投影，当前attempt join及子档保持真实；非空父引用不能降格为原首片无父。
    # 函数用途: 精确复现存在父ID却读不到父记录的坏账，不修改数据库或宿主记录。
    def missing_parent(identifier):
        if identifier == 'missing-db-parent':
            return None
        row = original_get(identifier)
        return {**dict(row), 'parent_agent_run_id': 'missing-db-parent'} if row is not None else None

    monkeypatch.setattr(repo, 'get_agent_run', missing_parent)
    with pytest.raises(ValueError, match='原父绑定不一致'):
        references_for_run(agent, actor)


@pytest.mark.parametrize('fault', ['wrong_bound_turn', 'wrong_stask_ref', 'terminal'])
def test_root_reference_requires_exact_original_session_input(tmp_path, monkeypatch, fault):
    from dataclasses import replace

    from agent_py_agent.agent.gateway_parts.io import write_json_file_atomic

    agent, actor, _, task, _, _ = delegated_case(tmp_path, monkeypatch)
    child = agent.subagents.list_runs()[0]
    select_child(agent, child)
    if fault == 'wrong_stask_ref':
        link = agent.conversation_store.tasks.load(task.task_id)
        write_json_file_atomic(agent.conversation_store.storage.task_path(task.task_id),
            replace(link, host_message_context={'source': 'session_task', 'task_id': 'other-stask'}).to_dict())
    else:
        updated = replace(task, status='accepted', conversation_request_id='wrong-turn') if fault == 'wrong_bound_turn' else replace(task, status='done')
        write_json_file_atomic(agent.conversation_store.session_tasks._path(task.task_id), updated.to_dict())
    with pytest.raises(ValueError):
        references_for_run(agent, actor)


def test_inline_context_is_frozen_in_original_task_link(tmp_path):
    from agent_py_agent.agent.adapter.protocol import provider_message_reference
    from agent_py_agent.agent.agent_core.runtime.loop_models import RunParams
    from agent_py_agent.agent.conversation.host_actor import actor_from_scope
    from agent_py_agent.agent.conversation.host_message_context import HostMessageContextBinding
    from agent_py_agent.agent.conversation.task_promotion import promote_current_conversation_task
    from agent_py_agent.tests.test_schedule_control import _agent

    agent = _agent(tmp_path, group='oc_group')
    source = agent.conversation_store.threads.get_or_create({'channel': 'feishu',
        'channel_conversation_id': 'oc_group', 'channel_user_id': 'ou_one', 'canonical_user_id': 'ou_one',
        'owner_id': agent.home_paths.owner_id, 'owner_home': str(agent.home_paths.owner_home_dir)})
    actor = actor_from_scope(agent, SimpleNamespace(channel='feishu', channel_user_id='ou_one',
        channel_conversation_id='oc_group'), source.thread_id, 'gwref-original')
    refs = ((provider_message_reference('om_one'), 'om_one'),)
    agent._current_run_params = RunParams(request_id=actor.entry_request_id, task_id=actor.entry_request_id,
        host_channel_actor=actor, host_message_references=refs,
        task_attributes={'conversation_thread_id': source.thread_id})
    link = promote_current_conversation_task(agent, goal='合成根工作')
    assert link is not None
    binding = HostMessageContextBinding.from_record(link.host_message_context)
    assert binding.snapshot.actor == actor and binding.snapshot.references == refs
    assert not binding.session_task_id


@pytest.mark.parametrize('has_new_refs', [False, True])
def test_same_goal_new_gateway_uses_only_this_input_and_preserves_original_root(tmp_path, monkeypatch, has_new_refs):
    from dataclasses import replace

    from agent_py_agent.agent.conversation.task_promotion import promote_current_conversation_task

    agent, original, refs, task, _, source = delegated_case(tmp_path, monkeypatch)
    link_before = agent.conversation_store.tasks.load(task.task_id).host_message_context
    actor = replace(original, entry_request_id='new-independent-gateway')
    new_refs = ((provider_message_reference('om_new_input'), 'om_new_input'),) if has_new_refs else ()
    # 同Goal的精确root是已选工作空间，不能用其原引用代替当前新消息；同owner/人工仍需本次入站身份。
    agent._current_run_params = RunParams(source='gateway', request_id=actor.entry_request_id, task_id=task.task_id,
        host_channel_actor=actor, host_message_references=new_refs,
        task_attributes={'conversation_thread_id': task.target_thread_id})
    assert references_for_run(agent, actor) == new_refs
    assert promote_current_conversation_task(agent).task_id == task.task_id
    assert agent.conversation_store.tasks.load(task.task_id).host_message_context == link_before
    agent._current_run_params.host_message_references = ()
    agent._current_run_params.task_attributes[CONTEXT_KEY] = {'references': refs}
    assert references_for_run(agent, actor) == ()
    assert source.thread_id == original.source_thread_id


def test_context_model_fields_do_not_become_typed_authority(tmp_path):
    from agent_py_agent.agent.conversation.host_message_context import HostMessageContext
    from agent_py_agent.agent.conversation.session_tasks import SessionTaskDraft
    from agent_py_agent.tests.test_schedule_control import _agent

    agent = _agent(tmp_path, group='oc_group')
    draft = SessionTaskDraft('source', 'target', '合成正文', dedupe_key='synthetic')
    task = agent.conversation_store.session_tasks.create(draft)
    assert CONTEXT_KEY not in task.metadata
    with pytest.raises(ValueError, match='上下文身份无效'):
        agent.conversation_store.session_tasks.create(draft, host_message_context={'actor': 'forged'})
    assert agent.conversation_store.session_tasks.load(task.task_id).metadata == {}
