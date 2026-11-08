# LLM: BG-5 回归只用隔离 home 与真实业务服务；后台装配的冻结 RunParams.business_context 必须让空间挂载能力生效。
# 模块用途: 验证 MOUNTED_CAPABILITIES 从运行参数取上下文、无宿主端口时经原本地业务服务读取空间。
from __future__ import annotations

import json
import os
from dataclasses import replace
from pathlib import Path
from uuid import uuid4

import pytest

from agent_py_agent.agent.agent_core.runtime.loop_models import RunParams
from agent_py_agent.agent.capability import business_capabilities
from agent_py_agent.agent.capability.business_capabilities import (
    _space_for_context,
    business_context_for_host,
)
from agent_py_agent.agent.conversation.business_contracts import BusinessExecutionContext
from agent_py_agent.agent.core import SimpleAgent
from agent_py_agent.agent.gateway_parts.business_api import (
    _model_business_actor,
    local_business_context,
)
from agent_py_agent.agent.gateway_parts.io import update_json_file_atomic
from agent_py_agent.agent.plugin_host.errors import HostAPIError
from agent_py_agent.agent.settings import AgentConfig
from agent_py_agent.tests._tool_runtime_harness import execute_registry_test_call


@pytest.fixture
def agent(tmp_path, monkeypatch):
    home = tmp_path / 'home'
    monkeypatch.setenv('MY_AGENT_HOME', str(home))
    host = SimpleAgent(AgentConfig(model_backend='echo', my_agent_home=str(home), memory_path='memory.jsonl',
        execution_mode='local_unmanaged'), tmp_path / 'workspace')
    monkeypatch.setattr(host, 'run', lambda *a, **k: pytest.fail('挂载能力解析不能调用模型'))
    return host


def _invoke(agent, action, params=None, **options):
    if 'idempotency_key' in options:
        options['idempotency_key'] = 'bg-w4-mounted-' + options['idempotency_key']
    outcome = execute_registry_test_call(agent.tools, 'business_' + action.replace('.', '_'),
        {'action': action, 'request_id': 'bg-w4', 'params': params or {}, **options}, call_id='bg-w4-' + uuid4().hex)
    return outcome, json.loads(outcome.output)


def _call(agent, action, params=None, **options):
    outcome, result = _invoke(agent, action, params, **options)
    assert outcome.ok, outcome.output
    return result['data']


def _task_turn(agent, task):
    context = BusinessExecutionContext.from_dict(task.metadata['business_context'])
    agent._current_run_params = RunParams(request_id='turn-' + uuid4().hex,
        task_attributes={'conversation_thread_id': task.target_thread_id}, business_context=context)
    return context


@pytest.fixture
def business_turn(agent):
    chat = agent.conversation_store.threads.get_or_create({
        'canonical_user_id': 'local-agent', 'channel': 'tui', 'channel_conversation_id': 'sess-bg-w4',
        'channel_user_id': 'local-agent', 'owner_id': agent.home_paths.owner_id,
        'owner_home': str(agent.home_paths.owner_home_dir)})
    agent._current_run_params = RunParams(request_id='turn-1', task_attributes={'conversation_thread_id': chat.thread_id})
    sid = _call(agent, 'spaces.create', {'name': '挂载空间'}, idempotency_key='space')['space_id']
    worker = _call(agent, 'background_agents.create', {'name': '工人', 'space_id': sid}, idempotency_key='worker')
    task_id = _call(agent, 'tasks.assign', {'agent_id': worker['agent']['agent_id'], 'space_id': sid,
        'instruction': '找合适的技能'}, idempotency_key='assign')['task_id']
    context = _task_turn(agent, agent.conversation_store.session_tasks.require(task_id))
    return sid, context


# LLM: 后台业务回合的冻结上下文在 RunParams 上，不在 task_attributes；总开关关闭仍保持原路由字节。
# 函数用途: 覆盖上下文回退读取与开关关闭返回 None。
def test_business_context_comes_from_frozen_run_params(agent, business_turn):
    sid, context = business_turn
    assert business_context_for_host(agent) == context and context.space_id == sid
    agent.config.business_spaces_enabled = False
    assert business_context_for_host(agent) is None


# LLM: SimpleAgent 没有 business_spaces/business_actor 端口；空间读取必须复用原本地业务服务与当前回合身份。
# 函数用途: 覆盖无端口宿主读取活动空间，以及路由搜索真实进入挂载能力解析。
def test_mounted_capabilities_resolve_without_host_ports(agent, business_turn, monkeypatch):
    sid, context = business_turn
    assert not hasattr(agent, 'business_spaces')
    space = _space_for_context(agent, context)
    assert space['space_id'] == sid and space['state'] == 'active'
    seen = []
    original = business_capabilities.resolve_business_capabilities

    def spy(**kwargs):
        seen.append(kwargs['context'].space_id)
        return original(**kwargs)

    monkeypatch.setattr(business_capabilities, 'resolve_business_capabilities', spy)
    agent.capability_router.search('报告', limit=5)
    assert seen == [sid]


# LLM: 未归属空间的业务任务（space_id=None）没有挂载能力；后台检索与包推荐必须沿原普通路由，不能抛错打断回合。
# 函数用途: 覆盖无空间业务上下文不进入挂载解析且 search 正常返回。
def test_space_less_business_task_keeps_plain_routing(agent, monkeypatch):
    chat = agent.conversation_store.threads.get_or_create({
        'canonical_user_id': 'local-agent', 'channel': 'tui', 'channel_conversation_id': 'sess-bg-w4-nospace',
        'channel_user_id': 'local-agent', 'owner_id': agent.home_paths.owner_id,
        'owner_home': str(agent.home_paths.owner_home_dir)})
    agent._current_run_params = RunParams(request_id='turn-1', task_attributes={'conversation_thread_id': chat.thread_id})
    worker = _call(agent, 'background_agents.create', {'name': '未分配', 'space_id': None}, idempotency_key='nospace')
    task_id = _call(agent, 'tasks.assign', {'agent_id': worker['agent']['agent_id'], 'space_id': None,
        'instruction': '随便看看'}, idempotency_key='nospace-assign')['task_id']
    context = BusinessExecutionContext.from_dict(
        agent.conversation_store.session_tasks.require(task_id).metadata['business_context'])
    assert context.space_id is None
    agent._current_run_params = RunParams(request_id='turn-2', task_attributes={'conversation_thread_id': worker['agent']['agent_id']},
        business_context=context)
    assert business_context_for_host(agent) is None
    monkeypatch.setattr(business_capabilities, 'resolve_business_capabilities',
                        lambda **_: pytest.fail('无空间上下文不能进入挂载解析'))
    agent.capability_router.search('报告', limit=5)
    agent.capability_router.search('报告', limit=0, kinds={'capability_package'})


def _delegated_task(agent, *, controller=False, follow_up=False):
    chat = agent.conversation_store.threads.get_or_create({
        'canonical_user_id': 'local-agent', 'channel': 'tui', 'channel_conversation_id': 'lineage',
        'channel_user_id': 'local-agent', 'owner_id': agent.home_paths.owner_id,
        'owner_home': str(agent.home_paths.owner_home_dir)})
    agent._current_run_params = RunParams(request_id='chat-turn',
        task_attributes={'conversation_thread_id': chat.thread_id})
    sid = _call(agent, 'spaces.create', {'name': '谱系空间'}, idempotency_key='lineage-space')['space_id']
    worker = _call(agent, 'background_agents.create', {'name': '末级工人', 'space_id': sid},
        idempotency_key='lineage-worker')['agent']['agent_id']
    if controller:
        root_id = _call(agent, 'tasks.submit', {'space_id': sid, 'instruction': '交给总控'},
            idempotency_key='lineage-root')['task_id']
    else:
        first = _call(agent, 'background_agents.create', {'name': '上级工人', 'space_id': sid},
            idempotency_key='lineage-first')['agent']['agent_id']
        root_id = _call(agent, 'tasks.assign', {'agent_id': first, 'space_id': sid, 'instruction': '交给工人'},
            idempotency_key='lineage-root')['task_id']
    root = agent.conversation_store.session_tasks.require(root_id)
    _task_turn(agent, root)
    child_id = _call(agent, 'tasks.assign', {'agent_id': worker, 'space_id': sid, 'instruction': '继续整理',
        'origin_task_id': root_id}, idempotency_key='lineage-child')['task_id']
    child = agent.conversation_store.session_tasks.require(child_id)
    if follow_up:
        next_id = _call(agent, 'tasks.follow_up', {'task_id': child_id, 'instruction': '补充进展'},
            idempotency_key='lineage-follow-up')['new_task_id']
        next_task = agent.conversation_store.session_tasks.require(next_id)
        assert next_task.origin_task_id == child_id
        assert (next_task.sender_thread_id, next_task.target_thread_id) == (
            child.sender_thread_id, child.target_thread_id)
        child = next_task
    return chat, sid, root, child


@pytest.mark.parametrize('controller', [False, True], ids=['worker-to-worker', 'controller-to-worker'])
@pytest.mark.parametrize('follow_up', [False, True], ids=['delegation', 'follow-up'])
def test_actual_worker_recovers_binding_from_canonical_lineage(agent, monkeypatch, controller, follow_up):
    chat, sid, root, task = _delegated_task(agent, controller=controller, follow_up=follow_up)
    context = _task_turn(agent, task)
    assert task.sender_thread_id == root.target_thread_id
    assert not agent.conversation_store.threads.require(task.sender_thread_id).channel_bindings
    space = _call(agent, 'spaces.get', {'space_id': sid})['space']
    assert space['space_id'] == sid and _space_for_context(agent, context)['space_id'] == sid
    actor = _model_business_actor(agent)
    assert actor.source_thread_id == task.target_thread_id
    assert actor.conversation_id == chat.channel_bindings[0].channel_conversation_id
    seen = []
    original = business_capabilities.resolve_business_capabilities

    def observe(**kwargs):
        seen.append(kwargs['context'].task_id)
        return original(**kwargs)

    monkeypatch.setattr(business_capabilities, 'resolve_business_capabilities', observe)
    agent.capability_router.search('报告', limit=5)
    assert seen == [task.task_id]


@pytest.mark.parametrize('defect', [
    'missing-parent', 'unreadable-parent', 'wrong-owner', 'wrong-endpoint', 'cycle', 'organization-only',
])
def test_invalid_or_unrecorded_lineage_stays_denied(agent, defect):
    chat, sid, root, task = _delegated_task(agent)
    store = agent.conversation_store
    if defect == 'unreadable-parent':
        store.session_tasks._path(root.task_id).write_text('{', encoding='utf-8')
    elif defect == 'wrong-owner':
        store.threads.update_atomic(task.sender_thread_id, lambda thread: replace(thread, owner_id='other-owner'))
    else:
        selected = root if defect == 'wrong-endpoint' else task
        changes = {'target_thread_id': chat.thread_id} if defect == 'wrong-endpoint' else {
            'origin_task_id': {'missing-parent': 'missing-parent', 'cycle': task.task_id,
                               'organization-only': ''}[defect]}
        update_json_file_atomic(store.session_tasks._path(selected.task_id),
            lambda data: {**data, **changes}, require_existing=True)
    context = _task_turn(agent, task)
    if defect == 'organization-only':
        assert store.threads.require(task.target_thread_id).metadata['background_agent']['managed_by_thread_id'] == chat.thread_id
    with pytest.raises(HostAPIError) as caught:
        local_business_context(agent, model=True)
    assert caught.value.code == 'CAPABILITY_DENIED' and caught.value.__cause__ is None
    outcome, result = _invoke(agent, 'spaces.get', {'space_id': sid})
    assert not outcome.ok and result['error']['code'] == 'CAPABILITY_DENIED'


@pytest.mark.skipif(os.name != 'posix', reason='需要原生 FD 目录')
def test_repeated_lineage_resolution_releases_all_descriptors(agent, record_testsuite_property):
    _, sid, _, task = _delegated_task(agent, controller=True, follow_up=True)
    _task_turn(agent, task)

    def exercise():
        assert _call(agent, 'spaces.get', {'space_id': sid})['space']['space_id'] == sid
        agent.capability_router.search('报告', limit=5)
        current = agent._current_run_params
        try:
            agent._current_run_params = replace(current,
                business_context=replace(current.business_context, owner_id='other-owner'))
            with pytest.raises(HostAPIError, match='CAPABILITY_DENIED'):
                local_business_context(agent, model=True)
        finally:
            agent._current_run_params = current

    exercise()
    root = Path('/proc/self/fd') if Path('/proc/self/fd').is_dir() else Path('/dev/fd')
    before = len(os.listdir(root))
    for _ in range(30):
        exercise()
    fd_delta = len(os.listdir(root)) - before
    record_testsuite_property('business_lineage_fd_delta', fd_delta)
    assert fd_delta == 0
