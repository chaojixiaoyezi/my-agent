"""覆盖现场首尾提及、已运行监控和无正文诊断，全部使用合成身份与隔离账本。"""
import errno
import json
from dataclasses import replace
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.adapter.protocol import feishu_to_incoming
from agent_py_agent.agent.conversation.control_commands import parse_conversation_control
from agent_py_agent.agent.gateway_parts.schedule_control import execute_schedule_control
from agent_py_agent.agent.scheduler.repository import SchedulerRunFinish
from agent_py_agent.agent.tooling.action_policy import ActionPolicy, ActionPolicyRequest
from agent_py_agent.agent.tooling.models import ApprovalPolicy
from agent_py_agent.tests._tool_runtime_harness import (
    canonical_test_call,
    make_test_model_spec,
    make_test_runtime_policy,
    runtime_snapshot_for_tools,
)
from agent_py_agent.tests.test_im_command_access import _payload
from agent_py_agent.tests.test_schedule_control import _agent, _job


@pytest.mark.parametrize('command', ['/schedule', '/schedule history job01', '/verbose stream', '/status', '/help', '/stop'])
@pytest.mark.parametrize('kind', ['text', 'post'])
@pytest.mark.parametrize('position', ['prefix', 'suffix', 'both'])
def test_bot_address_at_either_end_has_same_control_arguments(command, kind, position):
    mentions = [{'key': '@_user_1', 'id': {'open_id': 'ou_bot'}}]
    prefix, suffix = position in {'prefix', 'both'}, position in {'suffix', 'both'}
    if kind == 'text':
        body = {'text': ('@_user_1 ' if prefix else '') + command + (' @_user_1' if suffix else '')}
    else:
        nodes = ([{'tag': 'at', 'user_id': 'ou_bot'}] if prefix else [])
        nodes += [{'tag': 'text', 'text': ' ' + command + ' '}]
        nodes += ([{'tag': 'at', 'user_id': 'ou_bot'}] if suffix else [])
        body = {'post': {'zh_cn': {'content': [nodes]}}}
    incoming = feishu_to_incoming(_payload(kind, body, mentions), bot_open_id='ou_bot')
    assert incoming.content == command
    assert parse_conversation_control(incoming.content) == parse_conversation_control(command)


def test_address_removal_preserves_other_mentions_and_embedded_values():
    mentions = [{'key': '@_user_1', 'id': {'open_id': 'ou_bot'}}, {'key': '@_user_2', 'open_id': 'ou_other'}]
    for source, expected in [('/help @_user_2 @_user_1', '/help @_user_2'),
                             ('/admin synthetic@_user_1', '/admin synthetic@_user_1'),
                             ('你好 @_user_1', '你好 @_user_1')]:
        incoming = feishu_to_incoming(_payload('text', {'text': source}, mentions), bot_open_id='ou_bot')
        assert incoming.content == expected


def test_default_timezone_lists_started_monitor_and_real_history(tmp_path):
    from agent_py_agent.agent.gateway_parts.control_service import GatewayControlScope
    from agent_py_agent.agent.user_space.owner_resolver import OwnerIdentity

    agent = _agent(tmp_path, group='oc_group')
    job = _job(agent, 'ou_one', '已执行监控')
    repository = agent.scheduler_repository
    run = repository.reserve_manual_run(job['job_id'], now=1000)
    claim = repository.claim_run(run['run_id'], lease_seconds=60, now=1001)
    repository.mark_run_running(run['run_id'], claim['claim_id'], now=1002)
    repository.finish_run(run['run_id'], claim['claim_id'], SchedulerRunFinish(status='done', now=1003))
    scope = GatewayControlScope('ou_one', 'feishu', 'oc_group', resolved_owner=OwnerIdentity.provider_group('feishu', 'oc_group'))
    assert agent.config.timezone == ''
    listing = execute_schedule_control(agent, parse_conversation_control('/schedule'), scope)
    history = execute_schedule_control(agent, parse_conversation_control('/schedule history ' + job['alias']), scope)
    assert listing.ok and 'last:暂无' not in listing.message
    assert history.ok and 'done' in history.message


@pytest.mark.parametrize('number,code', [(errno.EACCES, 'SCHEDULER_PERMISSION_DENIED'),
    (errno.EROFS, 'SCHEDULER_READ_ONLY'), (errno.EAGAIN, 'SCHEDULER_STORE_BUSY')])
def test_schedule_failures_distinguish_errno_and_log_no_arguments(monkeypatch, caplog, number, code):
    import agent_py_agent.agent.gateway_parts.schedule_control as controls

    def fail(_agent, _scope):
        raise OSError(number, 'PRIVATE_EXCEPTION_BODY')

    monkeypatch.setattr(controls, 'visible_monitors', fail)
    result = execute_schedule_control(SimpleNamespace(), parse_conversation_control('/schedule'), None)
    assert not result.ok and result.error_code == code
    assert result.details['errno'] == number
    assert result.details['diagnostic_id'] in caplog.text
    assert 'PRIVATE_EXCEPTION_BODY' not in result.message + caplog.text
    assert '刷新' not in result.message


def test_invalid_schedule_arguments_do_not_read_owner_or_mutate(monkeypatch):
    def forbidden(*_args):
        raise AssertionError('无效控制不应查询或变更')

    monkeypatch.setattr('agent_py_agent.agent.gateway_parts.schedule_control.visible_monitors', forbidden)
    result = execute_schedule_control(SimpleNamespace(), parse_conversation_control('/schedule history job01 extra'), None)
    assert not result.ok and result.error_code == 'SCHEDULER_ARGUMENTS_INVALID'


@pytest.mark.parametrize('effect,expected', [('read_only', 'allow'), ('mutating', 'ask'), ('dangerous', 'ask')])
@pytest.mark.parametrize('mode', ['ask', 'auto'])
def test_effect_scoped_always_keeps_mutations_confirmed(tmp_path, effect, expected, mode):
    policy = replace(make_test_runtime_policy(effect), approval_policy=ApprovalPolicy('always', always_effects=('mutating', 'dangerous')))
    tool = SimpleNamespace(model_spec=make_test_model_spec('approval_probe'), runtime_policy=policy)
    snapshot = runtime_snapshot_for_tools({'approval_probe': tool})
    request = ActionPolicyRequest(canonical_test_call(snapshot, 'approval_probe', {}), snapshot, tmp_path, approval_mode=mode)
    assert ActionPolicy().decide(request).status == expected


def test_unknown_approval_effect_cannot_weaken_policy():
    with pytest.raises(ValueError):
        ApprovalPolicy('always', always_effects=('unknown',))
    with pytest.raises(ValueError):
        ApprovalPolicy('dangerous', always_effects=('mutating',))


def test_status_timing_roundtrip_preserves_unknown_and_rejects_text():
    from agent_py_agent.agent.conversation.control_commands import (
        ConversationTaskStatus,
        render_conversation_task_status,
    )
    from agent_py_agent.cli.chat_parts.control_runtime import _conversation_task_status_from_body

    facts = {'model_latency_ms': 2500, 'first_token_wait_ms': 1200, 'timed_model_call_count': 2,
             'physical_model_attempt_count': 3, 'first_token_timed_call_count': 2}
    status = ConversationTaskStatus(model_timing=facts)
    message = render_conversation_task_status(status)
    assert '2.5s' in message and '2/3' in message and '1.2s' in message
    assert '首包后' not in message
    parsed = _conversation_task_status_from_body({'model_timing': {**facts, 'secret': 'PRIVATE', 'post_first_token_ms': 'bad'}})
    assert parsed.model_timing == facts
    assert 'PRIVATE' not in render_conversation_task_status(parsed)


@pytest.mark.parametrize('facts', [
    {'timed_model_call_count': 1},
    {'model_latency_ms': 1000, 'timed_model_call_count': 1, 'first_token_timed_call_count': 1},
    {'model_latency_ms': 1000, 'timed_model_call_count': 1, 'split_latency_call_count': 1},
])
def test_partial_status_timing_is_unknown_instead_of_zero_or_keyerror(facts):
    from agent_py_agent.agent.gateway_parts.status_projection import render_model_timing

    text = render_model_timing(facts)
    assert '0.0s' not in text and '/0' not in text
    assert '未知' in text or '暂无完整计时' in text


def test_unresolved_monitor_lookup_keeps_partial_query_diagnostic(monkeypatch):
    monkeypatch.setattr('agent_py_agent.agent.gateway_parts.schedule_control.visible_monitors',
                        lambda *_: ([], ['SCHEDULER_CREATOR_UNRESOLVED']))
    result = execute_schedule_control(SimpleNamespace(), parse_conversation_control('/schedule job01'), None)
    assert not result.ok and result.error_code == 'SCHEDULER_NOT_FOUND'
    assert result.details['load_error_codes'] == ['SCHEDULER_CREATOR_UNRESOLVED']
    assert '旧创建人尚未确认' in result.message


@pytest.mark.parametrize('internal,details,expected', [
    ('SCHEDULER_STORE_UNREADABLE', {'errno': errno.EACCES}, 'SCHEDULER_PERMISSION_DENIED'),
    ('SCHEDULER_STORE_OWNER_MISMATCH', {}, 'SCHEDULER_STORE_OWNER_MISMATCH'),
    ('SCHEDULER_STORE_INVALID_CONTENT', {'line_no': 3, 'column': 2}, 'SCHEDULER_STORE_INVALID_CONTENT'),
])
def test_repository_wrapped_diagnostics_preserve_numeric_cause(monkeypatch, caplog, internal, details, expected):
    from agent_py_agent.agent.scheduler.repository import SchedulerRepositoryError

    def fail(*_):
        raise SchedulerRepositoryError('PRIVATE_BODY', error_code=internal,
                                       details={**details, 'path': 'PRIVATE_PATH', 'message': 'PRIVATE_BODY'})

    monkeypatch.setattr('agent_py_agent.agent.gateway_parts.schedule_control.visible_monitors', fail)
    result = execute_schedule_control(SimpleNamespace(), parse_conversation_control('/schedule'), None)
    assert result.error_code == expected
    if 'line_no' in details:
        assert result.details['line_no'] == 3 and result.details['column'] == 2
    assert 'PRIVATE' not in caplog.text + result.message + str(result.details)
