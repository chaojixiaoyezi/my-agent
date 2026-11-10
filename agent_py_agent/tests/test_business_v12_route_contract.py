# 模块用途: ROUTE-04 合同回归：/route true|false 经真实 Gateway 控制入口写入当前绑定线程并持久，受理快照冻结；
#   辅助路由只认严格结构化 kind，解释性文字、额外字段或未知能力不被解析或回退为聊天；chat 例外保持正常回合，
#   task 走耐久 SessionTask 与零工具接单。只用 tmp 隔离 home、脚本供应商与记录 IM，不是线上证据。
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

import agent_py_agent.tests.test_im_delegation_single_speaker as chain
from agent_py_agent.agent.conversation.control_commands import parse_conversation_command
from agent_py_agent.agent.conversation.store import ConversationStore
from agent_py_agent.agent.core import SimpleAgent
from agent_py_agent.agent.gateway_parts import im_task_routing
from agent_py_agent.agent.gateway_parts.control_service import (
    GatewayControlScope,
    execute_gateway_conversation_control,
)
from agent_py_agent.agent.gateway_parts.im_dispatch_policy import (
    IM_DISPATCH_KEY,
    capture_im_dispatch_for_owner,
)
from agent_py_agent.agent.gateway_parts.im_task_routing import prepare_im_task_dispatch
from agent_py_agent.agent.gateway_parts.io import write_json_file_atomic
from agent_py_agent.agent.runtime_errors import DataCorruptionError
from agent_py_agent.agent.settings import AgentConfig
from agent_py_agent.agent.user_space.owner_resolver import OwnerIdentity

_REAL_SELECT_ROUTE = im_task_routing._select_route


def _scope():
    return GatewayControlScope('ou_a', 'feishu', 'oc_a', resolved_owner=OwnerIdentity.provider_user('feishu', 'ou_a'))


def _control(agent, paths, text):
    result = execute_gateway_conversation_control(agent, paths, parse_conversation_command(text), _scope())
    assert result.ok, result.message
    return {key: result.details[key] for key in ('enabled', 'source', 'revision')}


def _capture(agent):
    return capture_im_dispatch_for_owner(home_paths=agent.home_paths, config=agent.config, scope=_scope(),
                                         capability_root=agent.root)


# 函数用途: 恢复真实结构化路由，只把辅助模型替换为按序脚本回复，并记录每次请求。
def _scripted_router(monkeypatch, *replies):
    seen, pending = [], iter(replies)

    def generate(request):
        seen.append(request)
        return SimpleNamespace(text=next(pending))

    monkeypatch.setattr(im_task_routing, '_select_route', _REAL_SELECT_ROUTE)
    monkeypatch.setattr(im_task_routing, 'generate_auxiliary_model_response', generate)
    return seen


def _tasks(agent):
    tasks, errors = agent.conversation_store.session_tasks.list_report(limit=0)
    assert not errors
    return tasks


def test_route_control_roundtrip_persists_on_bound_thread_and_accepted_snapshot_stays_frozen(tmp_path, monkeypatch):
    agent, paths, _provider, calls, _blocked = chain.ordinary_chain(tmp_path, monkeypatch)
    assert _control(agent, paths, '/route status') == {'enabled': False, 'source': 'default', 'revision': 0}
    assert _control(agent, paths, '/route true') == {'enabled': True, 'source': 'thread', 'revision': 1}
    agent.conversation_store = ConversationStore(agent.conversation_store.storage.root)
    assert _control(agent, paths, '/route status') == {'enabled': True, 'source': 'thread', 'revision': 1}
    accepted = _capture(agent)
    assert (accepted['mode'], accepted['route_mode_frozen'], accepted['preference_revision']) == ('delegated', True, 1)
    bound, error = agent.conversation_store.threads.resolve_report(
        channel='feishu', channel_conversation_id='oc_a', channel_user_id='ou_a')
    assert not error and accepted['route_origin']['thread_id'] == bound.thread_id
    assert _control(agent, paths, '/route false') == {'enabled': False, 'source': 'thread', 'revision': 2}
    later = _capture(agent)
    assert (later['mode'], later['route_mode_frozen'], later['preference_revision']) == ('original', False, 2)
    assert later['route_origin']['thread_id'] == bound.thread_id
    # 开关在受理后、执行前关闭：已受理请求仍按冻结快照委派，新请求按新设置走原聊天。
    monkeypatch.setattr(chain, 'capture_im_dispatch_for_owner', lambda **_: accepted)
    chain.ask(agent, paths, 'accepted', '整理工作区里的资料')
    assert len(_tasks(agent)) == 1 and calls[-1]['tools'] == [] and calls[-1]['thread'] == bound.thread_id
    monkeypatch.setattr(chain, 'capture_im_dispatch_for_owner', capture_im_dispatch_for_owner)
    chain.ask(agent, paths, 'after-off', '整理工作区里的资料')
    assert len(_tasks(agent)) == 1 and calls[-1]['tools'] and calls[-1]['thread'] == bound.thread_id


def test_route_true_prose_is_not_parsed_and_chat_exception_keeps_a_normal_turn(tmp_path, monkeypatch):
    agent, paths, _provider, calls, _blocked = chain.ordinary_chain(tmp_path, monkeypatch)
    _control(agent, paths, '/route true')
    seen = _scripted_router(monkeypatch, '这是个小的只读任务，我直接做。{"kind": "chat", "package_ids": []}',
                            '{"kind": "task", "package_ids": []}')
    chain.ask(agent, paths, 'small-read', '读一下 README 告诉我版本号')
    assert len(seen) == 2 and all(request.response_schema['properties']['kind']['enum'] == ['chat', 'task']
                                  for request in seen)
    [task] = _tasks(agent)
    assert task.goal == '读一下 README 告诉我版本号' and calls[-1]['tools'] == []
    seen = _scripted_router(monkeypatch, '{"kind": "chat", "package_ids": []}')
    chain.ask(agent, paths, 'schedule', '把每天九点的日报提醒改到十点')
    assert len(seen) == 1 and len(_tasks(agent)) == 1
    assert calls[-1]['tools'] and calls[-1]['thread'] == task.sender_thread_id


def _dispatch_context(tmp_path):
    agent = SimpleAgent(AgentConfig(prompt_files=[], enable_plugins=False, my_agent_home=str(tmp_path / 'home')),
                        tmp_path / 'workspace')
    source = agent.conversation_store.threads.get_or_create({
        'canonical_user_id': 'ou_a', 'channel': 'feishu', 'channel_user_id': 'ou_a',
        'channel_conversation_id': 'oc_a', 'owner_id': agent.home_paths.owner_id,
        'owner_home': str(agent.home_paths.owner_home_dir),
    })
    path = tmp_path / 'request.json'
    request = {IM_DISPATCH_KEY: {'mode': 'delegated', 'owner_id': agent.home_paths.owner_id, 'preference_revision': 1,
                                 'channel': 'feishu', 'conversation_id': 'oc_a', 'actor_id': 'ou_a'}}
    write_json_file_atomic(path, request)
    return agent, source, SimpleNamespace(agent=agent, request=request, request_path=path, request_id='input-one')


@pytest.mark.parametrize('replies', [
    ('我直接做这个小的只读任务。', '{"kind": "chat"}'),
    ('{"kind": "chat", "package_ids": [], "reason": "small read-only"}', '{"kind": "Chat", "package_ids": []}'),
    ('{"kind": "task", "package_ids": ["invented-pack"]}', '{"kind": "chat", "package_ids": "none"}'),
], ids=['prose', 'extra-field-and-case', 'invented-capability'])
def test_invalid_route_choice_fails_closed_without_chat_fallback_or_task(tmp_path, monkeypatch, replies):
    agent, source, context = _dispatch_context(tmp_path)
    seen = _scripted_router(monkeypatch, *replies)
    with pytest.raises(DataCorruptionError):
        prepare_im_task_dispatch(context, source, '读一下 README 告诉我版本号')
    assert len(seen) == 2 and _tasks(agent) == []
    assert im_task_routing.IM_ROUTING_KEY not in json.loads(context.request_path.read_text(encoding='utf-8'))


def test_chat_choice_is_persisted_once_and_replay_never_reselects(tmp_path, monkeypatch):
    agent, source, context = _dispatch_context(tmp_path)
    seen = _scripted_router(monkeypatch, '{"kind": "chat", "package_ids": []}')
    assert prepare_im_task_dispatch(context, source, '明天九点提醒我开会') is None
    marker = json.loads(context.request_path.read_text(encoding='utf-8'))[im_task_routing.IM_ROUTING_KEY]
    assert (marker['kind'], marker['package_refs'], marker['source_thread_id']) == ('chat', [], source.thread_id)
    assert prepare_im_task_dispatch(context, source, '明天九点提醒我开会') is None
    assert len(seen) == 1 and _tasks(agent) == []
    with pytest.raises(DataCorruptionError):
        prepare_im_task_dispatch(context, source, '换一个请求正文')
