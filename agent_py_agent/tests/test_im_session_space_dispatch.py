"""受理空间沿真实 IM 派发冻结进 SessionTask，不从旧展示字段授权。"""
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.core import SimpleAgent
from agent_py_agent.agent.gateway_parts.business_api import local_business_context
from agent_py_agent.agent.gateway_parts.im_dispatch_policy import IM_DISPATCH_KEY
from agent_py_agent.agent.gateway_parts.im_task_routing import prepare_im_task_dispatch
from agent_py_agent.agent.gateway_parts.io import write_json_file_atomic
from agent_py_agent.agent.gateway_parts.request_space import (
    REQUEST_SPACE_KEY,
    capture_request_space,
)
from agent_py_agent.agent.settings import AgentConfig


@pytest.fixture
def world(tmp_path, monkeypatch):
    agent = SimpleAgent(AgentConfig(my_agent_home=str(tmp_path / 'home'), business_spaces_enabled=True,
                                  prompt_files=[], enable_plugins=False), tmp_path / 'work')
    source = agent.conversation_store.threads.get_or_create({'canonical_user_id': 'ou_a',
        'channel': 'feishu', 'channel_user_id': 'ou_a', 'channel_conversation_id': 'oc_a',
        'owner_id': agent.home_paths.owner_id, 'owner_home': str(agent.home_paths.owner_home_dir)})
    scope = SimpleNamespace(channel='feishu', channel_user_id='ou_a', channel_conversation_id='oc_a')
    conversation = SimpleNamespace(thread_id=source.thread_id, scope=scope)
    authority, services = local_business_context(agent)
    actor = services.actor(authority)
    spaces = [services.spaces.create({'name': name}, actor=actor, idempotency_key='create-space-' + name)
              for name in ['alpha', 'beta']]
    from agent_py_agent.agent.conversation.session_space import set_session_space
    set_session_space(agent.home_paths, agent.conversation_store.threads, source, actor_id='ou_a',
        space_id=spaces[0]['space_id'], expected_revision=0, idempotency_key='select-alpha', authorize=lambda: None)
    source = agent.conversation_store.threads.require(source.thread_id)
    request = {IM_DISPATCH_KEY: {'mode': 'delegated', 'owner_id': agent.home_paths.owner_id,
        'preference_revision': 1, 'channel': 'feishu', 'conversation_id': 'oc_a', 'actor_id': 'ou_a',
        'space_id': spaces[1]['space_id']},
        REQUEST_SPACE_KEY: capture_request_space(agent.home_paths, source, actor_id='ou_a')}
    path = tmp_path / 'request.json'
    write_json_file_atomic(path, request)
    context = SimpleNamespace(agent=agent, request=request, request_path=path, request_id='request-first')
    monkeypatch.setattr('agent_py_agent.agent.gateway_parts.im_task_routing._select_route',
                        lambda *_: {'kind': 'task', 'package_refs': []})
    return context, conversation, spaces, services, actor


def test_im_freezes_selected_space_and_replays_original_context(world):
    context, conversation, spaces, services, actor = world
    task = prepare_im_task_dispatch(context, conversation, '整理材料')
    assert task.metadata['business_context']['space_id'] == spaces[0]['space_id']
    assert task.metadata['business_context']['task_id'] == task.task_id
    from agent_py_agent.agent.conversation.session_space import set_session_space
    source = context.agent.conversation_store.threads.require(conversation.thread_id)
    set_session_space(context.agent.home_paths, context.agent.conversation_store.threads, source,
        actor_id='ou_a', space_id=spaces[1]['space_id'], expected_revision=1,
        idempotency_key='select-beta', authorize=lambda: None)
    replay = prepare_im_task_dispatch(context, conversation, '整理材料')
    assert replay.task_id == task.task_id
    assert replay.metadata['business_context'] == task.metadata['business_context']
    # 归档后重放必须重核原空间，不能以旧受理成功绕过当前状态。
    services.spaces.active_references = lambda *_: []
    services.spaces.call('spaces.archive', {'space_id': spaces[0]['space_id']}, actor=actor,
                        expected_revision=0, idempotency_key='archive-alpha')
    with pytest.raises(ValueError, match='SPACE_ARCHIVED'):
        prepare_im_task_dispatch(context, conversation, '整理材料')


@pytest.mark.parametrize('field', ['owner_id', 'actor_id', 'thread_id'])
def test_im_rejects_mismatched_snapshot_before_dispatch(world, field):
    context, conversation, *_ = world
    context.request[REQUEST_SPACE_KEY][field] = 'foreign'
    with pytest.raises(ValueError, match='受理空间快照'):
        prepare_im_task_dispatch(context, conversation, '整理材料')


def test_legacy_display_space_does_not_authorize_business_context(world):
    context, conversation, *_ = world
    context.request.pop(REQUEST_SPACE_KEY)
    task = prepare_im_task_dispatch(context, conversation, '整理材料')
    assert 'business_context' not in task.metadata
    from agent_py_agent.agent.conversation.execution_threads import IM_TASK_EXECUTION_KEY
    policy = context.agent.conversation_store.threads.require(task.target_thread_id).metadata[IM_TASK_EXECUTION_KEY]
    assert policy['space_id'] == context.request[IM_DISPATCH_KEY]['space_id']
    assert prepare_im_task_dispatch(context, conversation, '整理材料').task_id == task.task_id


def test_chat_with_archived_selected_space_is_not_blocked(world, monkeypatch):
    context, conversation, spaces, services, actor = world
    services.spaces.active_references = lambda *_: []
    services.spaces.call('spaces.archive', {'space_id': spaces[0]['space_id']}, actor=actor,
                        expected_revision=0, idempotency_key='archive-before-chat')
    monkeypatch.setattr('agent_py_agent.agent.gateway_parts.im_task_routing._select_route',
                        lambda *_: {'kind': 'chat', 'package_refs': []})
    assert prepare_im_task_dispatch(context, conversation, '你好') is None


def test_null_snapshot_keeps_legacy_unassigned_behavior(world):
    context, conversation, *_ = world
    context.request[REQUEST_SPACE_KEY] = None
    task = prepare_im_task_dispatch(context, conversation, '整理材料')
    assert 'business_context' not in task.metadata


def test_dispatch_failure_preserves_code_effect_and_outcome(world, monkeypatch):
    from agent_py_agent.agent.conversation.session_task_dispatch import (
        SessionTaskDispatchError,
        session_task_dispatch_error,
    )
    context, conversation, *_ = world
    failure = session_task_dispatch_error('队列容量不足，请稍后重试。',
        error_code='SESSION_TASK_RATE_LIMIT', effect_outcome='not_started', details={'limit': 1})
    monkeypatch.setattr('agent_py_agent.agent.conversation.session_task_dispatch.dispatch_owned_background_task',
                        lambda *args, **kwargs: failure)
    with pytest.raises(SessionTaskDispatchError) as caught:
        prepare_im_task_dispatch(context, conversation, '整理材料')
    assert caught.value.error_code == failure.error_code
    assert failure.error_code != 'UNKNOWN_ERROR'
    assert caught.value.effect_outcome == 'not_started'
    assert caught.value.outcome is failure
