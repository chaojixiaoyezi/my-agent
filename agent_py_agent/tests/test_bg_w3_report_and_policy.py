# LLM: BG-3/BG-4 回归只用隔离 home 与真实 SessionTask/Wake/Guidance 原语；不调用模型，不新建队列或配置键。
# 模块用途: 验证业务派活终态唤醒发起会话，以及后台派活回合目录含只读/派活业务工具且不含自递归与编制变更。
from __future__ import annotations

import json
from dataclasses import replace
from uuid import uuid4

import pytest

from agent_py_agent.agent.agent_core.runtime.loop_models import RunParams
from agent_py_agent.agent.conversation.background_tool_policy import (
    BackgroundToolPolicyRequest,
    background_tool_policy_decision,
)
from agent_py_agent.agent.conversation.session_task_report import (
    report_task_result,
    result_report_key,
)
from agent_py_agent.agent.core import SimpleAgent
from agent_py_agent.agent.settings import AgentConfig
from agent_py_agent.tests._tool_runtime_harness import execute_registry_test_call

SESSION_TASK_BUSINESS = {
    'business_space', 'business_execution',
    'business_space:spaces.get', 'business_space:spaces.list',
    'business_execution:background_agents.list', 'business_execution:background_agents.get',
    'business_execution:tasks.assign', 'business_execution:tasks.follow_up',
    'business_execution:tasks.get', 'business_execution:tasks.list',
    'business_execution:messages.list',
}
NEVER_IN_BACKGROUND = {
    'business_activity', 'business_assets_versions_accept',
    'business_execution:tasks.submit', 'business_execution:background_agents.create',
    'business_execution:background_agents.retire', 'business_execution:background_agents.suspend',
    'business_execution:background_agents.resume',
    'business_space:spaces.create', 'business_space:spaces.update',
}


@pytest.fixture
def agent(tmp_path, monkeypatch):
    home = tmp_path / 'home'
    monkeypatch.setenv('MY_AGENT_HOME', str(home))
    host = SimpleAgent(AgentConfig(model_backend='echo', my_agent_home=str(home), memory_path='memory.jsonl',
        execution_mode='local_unmanaged'), tmp_path / 'workspace')
    monkeypatch.setattr(host, 'run', lambda *a, **k: pytest.fail('回报与目录计算不能调用模型'))
    return host


def _call(agent, action, params=None, **options):
    if 'idempotency_key' in options:
        options['idempotency_key'] = 'bg-w3-report-' + options['idempotency_key']
    outcome = execute_registry_test_call(agent.tools, 'business_' + action.replace('.', '_'),
        {'action': action, 'request_id': 'bg-w3', 'params': params or {}, **options}, call_id='bg-w3-' + uuid4().hex)
    assert outcome.ok, outcome.output
    return json.loads(outcome.output)['data']


def _assigned_task(agent):
    chat = agent.conversation_store.threads.get_or_create({
        'canonical_user_id': 'local-agent', 'channel': 'tui', 'channel_conversation_id': 'sess-bg-w3',
        'channel_user_id': 'local-agent', 'owner_id': agent.home_paths.owner_id,
        'owner_home': str(agent.home_paths.owner_home_dir)})
    agent._current_run_params = RunParams(request_id='turn-1', task_attributes={'conversation_thread_id': chat.thread_id})
    sid = _call(agent, 'spaces.create', {'name': '回报空间'}, idempotency_key='space')['space_id']
    worker = _call(agent, 'background_agents.create', {'name': '工人', 'space_id': sid}, idempotency_key='worker')
    task_id = _call(agent, 'tasks.assign', {'agent_id': worker['agent']['agent_id'], 'space_id': sid,
        'instruction': '整理日报'}, idempotency_key='assign')['task_id']
    return chat, agent.conversation_store.session_tasks.require(task_id)


def _sender_wakes(agent, sender):
    return [wake for wake in agent.conversation_store.wakes.pending(limit=0) if wake.thread_id == sender]


# LLM: 业务派活终态必须主动唤醒原发起会话，让 TUI/IM 立刻看到结果；同键重复收口只留一个唤醒。
# 函数用途: 覆盖业务任务唤醒形状、去重和 guidance 同时写入。
def test_business_task_result_wakes_sender_once(agent):
    chat, task = _assigned_task(agent)
    done = replace(task, status='done', summary='日报已整理')
    assert report_task_result(agent, done) is True
    assert report_task_result(agent, done) is True
    wakes = _sender_wakes(agent, chat.thread_id)
    assert [(wake.reason, wake.dedupe_key) for wake in wakes] == [('session_message', result_report_key(done))]
    assert wakes[0].metadata['origin_thread_id'] == task.target_thread_id
    pending, errors = agent.conversation_store.guidance.pending_report('thread', chat.thread_id, limit=0)
    assert not errors and [row.delivery for row in pending] == ['next_turn']


# LLM: 普通会话派活（无业务上下文）保持原 next_turn 合同；发起会话已归档时业务回报照写但不唤醒。
# 函数用途: 覆盖非业务任务不唤醒、归档发送方不唤醒且返回成功。
def test_plain_session_task_and_archived_sender_keep_original_contract(agent):
    chat, task = _assigned_task(agent)
    plain = replace(task, task_id='stask-plain-' + uuid4().hex[:12], status='done', metadata={})
    assert report_task_result(agent, plain) is True
    assert _sender_wakes(agent, chat.thread_id) == []
    agent.conversation_store.threads.write(replace(agent.conversation_store.threads.require(chat.thread_id),
                                                   status='archived'))
    assert report_task_result(agent, replace(task, status='failed', summary='')) is True
    assert _sender_wakes(agent, chat.thread_id) == []


def _allowed(agent, reason):
    decision = background_tool_policy_decision(agent.config,
        request=BackgroundToolPolicyRequest(reason=reason, config=agent.config))
    return set(decision.allowed_tools)


# LLM: 后台代理/总控接单回合要能读团队并再派活；tasks.submit 与编制变更不进后台目录，其它唤醒档不受影响。
# 函数用途: 覆盖 session_task 目录增量、排除项及 default/goal/urgent 不含业务工具。
def test_session_task_wake_profile_carries_business_dispatch_tools(agent):
    session_task = _allowed(agent, 'session_task')
    assert session_task >= SESSION_TASK_BUSINESS
    assert not NEVER_IN_BACKGROUND & session_task
    for reason in ('', 'urgent', 'thread_goal_continue', 'audit_finding'):
        assert not {name for name in _allowed(agent, reason) if name.startswith('business_')}, reason
    assert set(agent.tools.tools) >= {'business_space', 'business_execution'}, '白名单域工具名必须是真实注册的业务工具名'
