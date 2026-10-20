# LLM: 共用回复槽先判定是否存在结构化工具回执候选；本文件只测试读取边界，真实操作、身份及暂停门由原集成测试覆盖。
# 模块用途: 防止没有工具回执的后台收口提前认领来源身份，同时证明候选仍必须经过原身份与来源核验。
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.conversation.background_delivery import committed_source_replies
from agent_py_agent.agent.conversation.channels import DeliveryContext
from agent_py_agent.agent.conversation.host_actor import HostActorError


# LLM: 这里只提供候选形状，不提供canonical操作或授权；不得把此字典当成已发送事实。
# 函数用途: 为候选有无与核验顺序测试建立最小回执形状。
def _candidate():
    return {'schema_version': 'message_tool_delivery.v1', 'delivery_status': 'sent',
        'source_owner_delivery': True, 'receipt_id': 'synthetic-receipt', 'channel': 'feishu',
        'content': 'synthetic', 'reply_binding': {'operation_id': 'synthetic-operation'}}


# LLM: 不装配存储或运输，任何无候选路径越过边界都会直接暴露；没有提供可被借用的身份。
# 函数用途: 给纯筛选入口提供最小本轮请求、目标及执行编号。
def _inputs():
    return (SimpleNamespace(), SimpleNamespace(thread_id='execution-thread', task_id='task'),
        DeliveryContext(channel='feishu', target='synthetic-target', thread_id='source-thread'),
        {'request_id': 'request', 'run_id': 'run', 'task_id': 'task', 'attempt_id': 'attempt'})


@pytest.mark.parametrize('shape', ['empty', 'not_record', 'legacy_flag', 'no_binding',
    'no_operation', 'failed', 'other_channel', 'no_content'])
def test_no_source_reply_candidate_does_not_read_actor_or_origin(monkeypatch, shape):
    item = _candidate()
    if shape == 'legacy_flag':
        item = {'delivery_status': 'sent', 'source_owner_delivery': True}
    elif shape == 'no_binding':
        item.pop('reply_binding')
    elif shape == 'no_operation':
        item['reply_binding'] = {}
    elif shape == 'failed':
        item['delivery_status'] = 'failed'
    elif shape == 'other_channel':
        item['channel'] = 'other-channel'
    elif shape == 'no_content':
        item['content'] = ''
    deliveries = () if shape == 'empty' else (None,) if shape == 'not_record' else (item,)

    def forbidden(*_args):
        pytest.fail('无来源回复候选不得读取发起人或触发调度来源核验')

    monkeypatch.setattr('agent_py_agent.agent.conversation.host_actor.background_host_actor', forbidden)
    monkeypatch.setattr('agent_py_agent.agent.scheduler.execution.validate_delivery_origin', forbidden)
    agent, request, context, identity = _inputs()
    assert committed_source_replies(agent, request, context, deliveries, execution_identity=identity) == ()


@pytest.mark.parametrize('code', ['CHANNEL_ACTOR_IDENTITY_INVALID', 'CHANNEL_ACTOR_SCOPE_MISMATCH'])
def test_source_reply_candidate_keeps_actor_failure(monkeypatch, code):
    error = HostActorError(code)

    def invalid_actor(*_args):
        raise error

    monkeypatch.setattr('agent_py_agent.agent.conversation.host_actor.background_host_actor', invalid_actor)
    agent, request, context, identity = _inputs()
    with pytest.raises(HostActorError) as raised:
        committed_source_replies(agent, request, context, (_candidate(),), execution_identity=identity)
    assert raised.value is error


def test_source_reply_candidate_keeps_origin_rejection(monkeypatch):
    calls = []

    def actor(*_args):
        calls.append('actor')
        return object()

    def rejected(*_args):
        calls.append('origin')
        return False

    monkeypatch.setattr('agent_py_agent.agent.conversation.host_actor.background_host_actor', actor)
    monkeypatch.setattr('agent_py_agent.agent.scheduler.execution.validate_delivery_origin', rejected)
    agent, request, context, identity = _inputs()
    assert committed_source_replies(agent, request, context, (None, _candidate()), execution_identity=identity) == ()
    assert calls == ['actor', 'origin']
