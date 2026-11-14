"""普通无空间SessionTask追问沿原Owner/来源/谱系，不伪造业务DTO或默认空间。"""
import json
from dataclasses import replace
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.conversation.background_agents import BackgroundAgentService
from agent_py_agent.agent.conversation.business_access import BusinessError
from agent_py_agent.agent.conversation.execution_threads import (
    IM_TASK_EXECUTION_KEY,
    bind_delegated_task,
    ensure_execution_thread,
)
from agent_py_agent.agent.conversation.session_tasks import SessionTaskDraft, SessionTaskUpdate
from agent_py_agent.tests.test_business_v12_background_scheduling import (  # noqa: F401
    assigned_task,
    call,
    native,
)
from agent_py_agent.tests.test_unassigned_session_registered_artifacts import prepared


def test_original_im_task_has_no_synthetic_space_context(tmp_path):
    w = prepared(tmp_path)
    service = BackgroundAgentService(w.agent)
    task, context = service._owned_task(w.task.task_id, w.agent._current_run_params.host_channel_actor)
    assert context is None and task.metadata.get('business_context') is None


@pytest.mark.parametrize('change', ['space', 'source', 'home', 'origin', 'missing'])
def test_unassigned_followup_refuses_changed_authority_before_dispatch(tmp_path, change):
    w = prepared(tmp_path)
    service = BackgroundAgentService(w.agent)
    actor = w.agent._current_run_params.host_channel_actor
    task_id = w.task.task_id
    if change == 'space':
        service.call_context = SimpleNamespace(selected_space_id='space-explicit')
    elif change == 'source':
        actor = replace(actor, source_thread_id=w.task.target_thread_id)
    elif change == 'home':
        w.store.threads.update_atomic(w.task.target_thread_id, lambda t: replace(t, owner_home=str(tmp_path/'foreign')))
    elif change == 'origin':
        from agent_py_agent.agent.gateway_parts.io import update_json_file_atomic
        update_json_file_atomic(w.store.session_tasks._path(task_id), lambda d: {**d, 'origin_task_id': 'missing-origin'})
    else:
        task_id = 'stask-missing'
    with pytest.raises(BusinessError) as caught:
        service._owned_task(task_id, actor)
    assert caught.value.effect_outcome == 'not_started'
    assert caught.value.code in {'RESOURCE_NOT_FOUND', 'CAPABILITY_DENIED', 'SPACE_SCOPE_MISMATCH'}
    assert caught.value.details['task_created'] is False


def test_normal_gateway_followup_creates_new_unassigned_task_and_preserves_old(native):  # noqa: F811
    _, host = native
    _, original = assigned_task(native, name='source-only')
    sender_id = host.conversation_store.session_tasks.require(original).sender_thread_id
    target = ensure_execution_thread(host, sender_id, 'legacy-followup', kind='delegated')
    host.conversation_store.threads.update_atomic(target.thread_id, lambda t: replace(t,
        metadata={**t.metadata, IM_TASK_EXECUTION_KEY: {'mode': 'delegated'}}))
    old = host.conversation_store.session_tasks.create(SessionTaskDraft(sender_id, target.thread_id, '原普通任务'))
    bind_delegated_task(host.conversation_store, target.thread_id, old.task_id)
    host.conversation_store.session_tasks.bind_turn(old.task_id, turn_id='old-turn')
    host.conversation_store.session_tasks.advance(old.task_id, SessionTaskUpdate(status='done'))
    before = host.conversation_store.session_tasks._path(old.task_id).read_bytes()
    status, response = call(native, 'tasks.follow_up', {'task_id': old.task_id, 'instruction': '读取原结果，不修改。'},
        idempotency_key='legacy_followup_wire_0001')
    assert status == 200 and response["effect_outcome"] == "committed", json.dumps(response, ensure_ascii=False)
    new_id = response['data']['new_task_id']
    new = host.conversation_store.session_tasks.require(new_id)
    assert new_id != old.task_id and new.origin_task_id == old.task_id
    assert new.sender_thread_id == sender_id and new.target_thread_id != target.thread_id
    current = host.conversation_store.threads.require(new.target_thread_id)
    assert IM_TASK_EXECUTION_KEY not in current.metadata
    assert host.conversation_store.threads.require(target.thread_id).metadata[IM_TASK_EXECUTION_KEY]['session_task_id'] == old.task_id
    assert new.metadata['business_context']['space_id'] is None
    link = host.conversation_store.tasks.load(new_id)
    assert link.host_channel_actor['source_thread_id'] == sender_id
    assert link.thread_id == new.target_thread_id and not link.task_path
    original_actor = dict(link.host_channel_actor)
    from agent_py_agent.agent.conversation.host_actor import HostChannelActor

    service = BackgroundAgentService(host)
    service.call_context = SimpleNamespace(selected_space_id='space-explicit')
    with pytest.raises(BusinessError) as blocked:
        service._owned_task(new_id, HostChannelActor.from_record(original_actor))
    assert blocked.value.code == 'SPACE_SCOPE_MISMATCH' and blocked.value.effect_outcome == 'not_started'
    assert response['data']['prior_task_ref'] == old.task_id
    assert host.conversation_store.session_tasks._path(old.task_id).read_bytes() == before
    replay_status, replay = call(native, 'tasks.follow_up', {'task_id': old.task_id, 'instruction': '读取原结果，不修改。'},
        idempotency_key='legacy_followup_wire_0001')
    assert replay_status == 200 and replay['data']['new_task_id'] == new_id
    assert host.conversation_store.tasks.load(new_id).host_channel_actor == original_actor
    bad_status, bad = call(native, 'tasks.follow_up', {'task_id': old.task_id, 'instruction': '读取', 'space_id': 'explicit-space'},
        idempotency_key='legacy_followup_bad_space_0001')
    assert bad_status == 400 and bad['effect_outcome'] == 'not_started'



def test_dispatch_rejects_untyped_actor_before_any_task_side_effect(tmp_path):
    from agent_py_agent.agent.capability.config import CapabilityConfig
    from agent_py_agent.agent.conversation.session_task_dispatch import (
        SessionTaskDispatchPlan,
        dispatch_session_task,
    )

    w = prepared(tmp_path)
    actor = w.agent._current_run_params.host_channel_actor
    before = [task.task_id for task in w.store.session_tasks.list_report()[0]]
    plan = SessionTaskDispatchPlan(w.task.sender_thread_id, w.task.target_thread_id, '不应派发', idempotency_key='typed-only')
    with pytest.raises(BusinessError) as caught:
        dispatch_session_task(w.agent, plan, w.store.threads.require(w.task.target_thread_id), CapabilityConfig(),
            host_channel_actor=actor.to_record())
    assert caught.value.effect_outcome == 'not_started' and caught.value.code == 'CAPABILITY_DENIED'
    assert [task.task_id for task in w.store.session_tasks.list_report()[0]] == before


def test_dispatch_actor_link_failure_resumes_same_task_before_body_and_wake(tmp_path, monkeypatch):
    from agent_py_agent.agent.capability.config import CapabilityConfig
    from agent_py_agent.agent.conversation.session_task_dispatch import (
        SessionTaskDispatchPlan,
        dispatch_session_task,
    )

    w = prepared(tmp_path)
    actor = w.agent._current_run_params.host_channel_actor
    target = ensure_execution_thread(w.agent, w.task.sender_thread_id, 'fresh-followup', kind='delegated')
    plan = SessionTaskDispatchPlan(w.task.sender_thread_id, target.thread_id, '复核原件', w.task.task_id, 'resume-key')
    original = w.store.tasks.bind

    def fail_before_link(*args, **kwargs):
        raise OSError('test_link_write_interrupted')

    monkeypatch.setattr(w.store.tasks, 'bind', fail_before_link)
    first = dispatch_session_task(w.agent, plan, target, CapabilityConfig(), host_channel_actor=actor)
    assert not first.ok and first.effect_outcome == 'unknown'
    task_id = first.result_envelope['session_task_dispatch']['task_id']
    assert not w.store.session_tasks.require(task_id).body_guidance_id
    assert w.store.tasks.load(task_id) is None
    monkeypatch.setattr(w.store.tasks, 'bind', original)
    replay_actor = replace(actor, entry_request_id='trusted-replay-entry')
    resumed = dispatch_session_task(w.agent, plan, target, CapabilityConfig(), host_channel_actor=replay_actor)
    assert resumed.ok and json.loads(resumed.output)['task_id'] == task_id
    assert w.store.session_tasks.require(task_id).body_guidance_id
    assert w.store.tasks.load(task_id).host_channel_actor == replay_actor.to_record()
    again = dispatch_session_task(w.agent, plan, target, CapabilityConfig(),
        host_channel_actor=replace(actor, entry_request_id='later-entry'))
    assert again.ok and json.loads(again.output)['task_id'] == task_id
    assert w.store.tasks.load(task_id).host_channel_actor == replay_actor.to_record()


def test_terminal_dispatch_replay_does_not_create_late_actor_or_body(tmp_path, monkeypatch):
    from agent_py_agent.agent.capability.config import CapabilityConfig
    from agent_py_agent.agent.conversation.session_task_dispatch import (
        SessionTaskDispatchPlan,
        dispatch_session_task,
    )

    w = prepared(tmp_path)
    actor = w.agent._current_run_params.host_channel_actor
    target = ensure_execution_thread(w.agent, w.task.sender_thread_id, 'cancelled-followup', kind='delegated')
    plan = SessionTaskDispatchPlan(w.task.sender_thread_id, target.thread_id, '原件复核', w.task.task_id, 'cancelled-key')

    def forbidden_bind(*args, **kwargs):
        raise OSError('test_link_interrupted')

    monkeypatch.setattr(w.store.tasks, 'bind', forbidden_bind)
    first = dispatch_session_task(w.agent, plan, target, CapabilityConfig(), host_channel_actor=actor)
    task_id = first.result_envelope['session_task_dispatch']['task_id']
    w.store.session_tasks.advance(task_id, SessionTaskUpdate(status='cancelled'))
    before = w.store.session_tasks._path(task_id).read_bytes()
    replay = dispatch_session_task(w.agent, plan, target, CapabilityConfig(), host_channel_actor=actor)
    assert replay.ok and json.loads(replay.output)['status'] == 'cancelled'
    assert w.store.tasks.load(task_id) is None
    assert w.store.session_tasks._path(task_id).read_bytes() == before
