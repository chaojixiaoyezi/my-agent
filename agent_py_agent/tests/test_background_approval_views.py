"""显式后台会话兴趣不授票，双界面独立，离开/失效不复活旧决定。"""
import json
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.conversation import background_approval_views as views
from agent_py_agent.tests.test_background_tool_approval import background as background
from agent_py_agent.tests.test_mega_business_api_actions import background_http as background_http
from agent_py_agent.tests.test_mega_business_api_actions import native_request


@pytest.mark.parametrize('permission_active,agent_id,expected', [(True, 'bg-own', 1), (True, '', 0), (False, 'bg-own', 0)])
def test_permission_back_only_leaves_selected_background(monkeypatch, permission_active, agent_id, expected):
    from prompt_toolkit.filters import Always, Never
    from prompt_toolkit.key_binding import KeyBindings
    from prompt_toolkit.keys import Keys

    from agent_py_agent.cli.chat_parts import tui_keybindings as bindings

    events = []
    monkeypatch.setattr(bindings, '_handle_agent_back_keybinding', lambda event, params: events.append('back'))
    monkeypatch.setattr(bindings, '_cancel_permission', lambda *args: pytest.fail('返回不能取消审批'))
    params = SimpleNamespace(agent_navigation=SimpleNamespace(snapshot=lambda: SimpleNamespace(active_agent_id=agent_id)))
    fields = {name: Never() for name in bindings._TuiBindingFilters.__dataclass_fields__}
    fields['permission_active'] = Always() if permission_active else Never()
    kb = KeyBindings()
    bindings._register_permission_bindings(kb, params, bindings._TuiBindingFilters(**fields))
    for binding in kb.get_bindings_for_keys((Keys.ControlG,)):
        if binding.filter():
            binding.handler(None)
    assert events == ['back'] * expected


def test_taskless_background_sink_uses_original_published_execution(background):
    from agent_py_agent.agent.contracts.tool_approval import ToolApprovalDecision
    from agent_py_agent.agent.conversation.agent_control import resolve_agent_permission
    from agent_py_agent.tests.test_background_tool_approval import (
        _bind_background_authority,
        _pending_request,
    )

    ctx = background
    _bind_background_authority(ctx)
    ctx.sink._transcript.task_id = ''
    rows = _pending_request(ctx)
    assert rows[0]['run_id'] == ctx.task_id
    resolve_agent_permission(ctx.agent, scope=ctx.scope, run_id=ctx.task_id, request=ctx.request.to_dict(),
        decision=ToolApprovalDecision(ctx.request.permission_id, 'denied').to_dict())
    ctx.waiter.join(timeout=2)
    assert ctx.response['decision'] == 'denied'


@pytest.mark.parametrize('claim_task,has_authority,expected', [('', True, True), ('', False, False), ('foreign-task', True, False)])
def test_raw_wake_claim_requires_original_core_publication(background, claim_task, has_authority, expected):
    from agent_py_agent.agent.common.json_io import (
        locked_json_path,
        read_json_object_report,
        write_json_file_atomic_unlocked,
    )
    from agent_py_agent.agent.conversation.tool_approval_scope import tool_approval_scope
    from agent_py_agent.tests.test_background_tool_approval import _bind_background_authority

    ctx = background
    params = _bind_background_authority(ctx)
    # 仅变异原领取记录的前置任务事实；authority仍来自原生产binder，不在测试里造运行票。
    path = ctx.store.storage.background_claim_path(ctx.thread.thread_id)
    with locked_json_path(path):
        row = read_json_object_report(path, context='qa.raw.claim').payload
        row['task_id'] = claim_task
        if not has_authority:
            row.pop('runtime_authority', None)
        write_json_file_atomic_unlocked(path, row)
    assert tool_approval_scope(ctx.agent, params.run_id).active is expected


@pytest.mark.parametrize('mutation', ['untyped', 'wrong_thread', 'closed_control', 'wrong_attempt'])
def test_taskless_background_sink_rejects_unbound_execution(background, mutation):
    from dataclasses import replace

    from agent_py_agent.agent.conversation.agent_tool_approval import _sink_approval_run_id
    from agent_py_agent.tests.test_background_tool_approval import _bind_background_authority

    ctx = background
    params = _bind_background_authority(ctx)
    if mutation == 'untyped':
        ctx.agent._current_run_params = SimpleNamespace(**params.__dict__)
    elif mutation == 'wrong_thread':
        ctx.agent._current_run_params = replace(params, task_attributes={'conversation_thread_id': 'foreign-thread'})
    elif mutation == 'closed_control':
        params.conversation_task_binding_callback.finish()
    else:
        ctx.agent._current_run_params = replace(params, attempt_id='foreign-attempt')
    with pytest.raises(ValueError):
        _sink_approval_run_id(ctx.agent, '', ctx.thread.thread_id)


def test_scope_failure_diagnostic_roundtrip_is_bounded_and_keeps_not_started():
    from agent_py_agent.agent.agent_core.tool_loop.round_execution import (
        _rejected_approval_execution,
    )
    from agent_py_agent.agent.contracts.tool_approval import ToolApprovalDecision
    from agent_py_agent.agent.tooling.action_policy import ActionDecision
    from agent_py_agent.agent.tooling.executor import ToolExecution
    from agent_py_agent.agent.tooling.runtime_contracts import ToolCall, ToolResult

    call = ToolCall(call_id='scope-case', tool_name='run_command', arguments={}, source_protocol='native',
        schema_hash='sha256:scope', run_id='real-root', turn_id='real-turn', attempt_id='real-attempt')
    decision = ToolApprovalDecision('approval:scope', 'unavailable', reason_code='APPROVAL_SCOPE_INVALID',
        diagnostic='ValueError: execution binding missing ' + 'x' * 600)
    decision = ToolApprovalDecision.from_mapping(decision.to_dict())
    assert len(decision.diagnostic) == 512
    execution = ToolExecution(call, ActionDecision('ask', ('APPROVAL_REQUIRED',), {}),
        ToolResult.failed(call, 'approval needed', error_code='APPROVAL_REQUIRED', failure_stage='authorization'), ())
    result = _rejected_approval_execution(execution, decision).result
    assert result.error_code == 'APPROVAL_SCOPE_INVALID'
    assert 'ValueError: execution binding missing' in result.output
    assert '工具主体未执行' in result.output and '盲重试' in result.output
    assert result.handler_executed is False


def call(native, path, body):
    status, _, raw = native_request(native, 'POST', path, body, user='local-agent',
                                   headers={'X-Channel': 'chat', 'X-Conversation-Id': body.get('conversation_id', 'view-source')})
    return status, json.loads(raw)


def target(native):
    from uuid import uuid4
    request = {'action': 'background_agents.create', 'request_id': uuid4().hex,
               'idempotency_key': uuid4().hex, 'params': {'space_id': None, 'name': 'approval-view-qa'}}
    status, result = call(native, '/client/business', request)
    assert status == 200, result
    return result['data']['agent']['agent_id']


def test_real_wire_idle_background_interest_is_explicit_and_two_views_close_independently(background_http):
    native, agent = background_http
    agent_id = target(background_http)
    common = {'conversation_id': 'view-source', 'operation': 'observe'}
    a, b = {'agent_id': agent_id, 'view_id': 'view-a'}, {'agent_id': agent_id, 'view_id': 'view-b'}
    assert not views.receiving(agent, agent_id)
    for selected in (a, b):
        status, result = call(background_http, '/client/agent-approval-view', {**common, 'view': selected})
        assert status == 200 and result['root_task_id'] == '' and result['coverage'] == 'complete', result
    assert views.receiving(agent, agent_id)
    assert call(background_http, '/client/agent-approval-view', {**common, 'operation': 'close', 'view': a})[0] == 200
    assert views.receiving(agent, agent_id)
    assert call(background_http, '/client/agent-approval-view', {**common, 'operation': 'close', 'view': b})[0] == 200
    assert not views.receiving(agent, agent_id)


def test_real_wire_list_and_wrong_source_cannot_claim_or_revoke_view(background_http):
    _, agent = background_http
    agent_id = target(background_http)
    selected = {'agent_id': agent_id, 'view_id': 'view-a'}
    body = {'conversation_id': 'view-source', 'operation': 'observe', 'view': selected}
    assert call(background_http, '/client/agent-approval-view', body)[0] == 200
    for payload in ({**body, 'conversation_id': 'other-session'},
                    {**body, 'view': {**selected, 'agent_id': 'not-a-real-thread'}},
                    {**body, 'view': {**selected, 'owner_id': 'other'}}):
        status, result = call(background_http, '/client/agent-approval-view', payload)
        assert status >= 400 and result.get('ok') is False, result
    assert views.receiving(agent, agent_id)


def test_view_expiry_and_original_binding_change_reject_old_decision(tmp_path, monkeypatch):
    from agent_py_agent.agent.conversation import ConversationStore
    from agent_py_agent.agent.conversation.models import ChannelBinding
    store = ConversationStore(tmp_path / 'conversation')
    source = store.threads.get_or_create({'channel': 'chat', 'channel_conversation_id': 'source',
        'channel_user_id': 'self', 'canonical_user_id': 'self', 'owner_id': 'local/main'})
    agent = SimpleNamespace(conversation_store=store, home_paths=SimpleNamespace(owner_id='local/main'))
    binding = {'view_id': 'view-a', 'source_thread_id': source.thread_id, 'actor_id': 'self', 'conversation_id': 'source'}
    views.renew(agent, 'target-thread', binding)
    assert views.receiving(agent, 'target-thread')
    with views.decision_scope(agent, 'target-thread', binding):
        pass
    views.close(agent, 'target-thread', binding)
    with pytest.raises(ValueError, match='INACTIVE'):
        with views.decision_scope(agent, 'target-thread', binding):
            pytest.fail('closed view entered original approval engine')
    views.renew(agent, 'target-thread', binding)
    monkeypatch.setattr(views.time, 'time', lambda: 10**12)
    assert not views.receiving(agent, 'target-thread')
    assert not views._path(agent, 'target-thread').exists()
    with pytest.raises(ValueError, match='INACTIVE'):
        with views.decision_scope(agent, 'target-thread', binding):
            pytest.fail('expired view entered original approval engine')


def test_expired_read_cannot_delete_concurrently_renewed_seat(tmp_path, monkeypatch):
    import threading

    from agent_py_agent.agent.conversation import ConversationStore
    store = ConversationStore(tmp_path / 'conversation')
    source = store.threads.get_or_create({'channel': 'chat', 'channel_conversation_id': 'source',
        'channel_user_id': 'self', 'canonical_user_id': 'self', 'owner_id': 'local/main'})
    agent = SimpleNamespace(conversation_store=store, home_paths=SimpleNamespace(owner_id='local/main'))
    binding = {'view_id': 'old-seat', 'source_thread_id': source.thread_id, 'actor_id': 'self', 'conversation_id': 'source'}
    views.renew(agent, 'target-thread', binding)
    monkeypatch.setattr(views.time, 'time', lambda: 10**12)
    observed, renewed = threading.Event(), threading.Event()
    read = views._read
    result = []

    def pause_expired_read(selected_agent, thread_id):
        row = read(selected_agent, thread_id)
        if threading.current_thread().name == 'expired-reader' and not observed.is_set():
            observed.set()
            assert renewed.wait(3)
        return row

    monkeypatch.setattr(views, '_read', pause_expired_read)
    worker = threading.Thread(name='expired-reader', target=lambda: result.append(views.receiving(agent, 'target-thread')))
    worker.start()
    try:
        assert observed.wait(3)
        views.renew(agent, 'target-thread', {**binding, 'view_id': 'new-seat'})
    finally:
        renewed.set()
        worker.join(timeout=3)
    assert not worker.is_alive()
    assert result == [True]
    assert set(read(agent, 'target-thread')) == {'new-seat'}


def test_interest_capacity_is_bounded_and_expired_seats_can_be_reclaimed(tmp_path, monkeypatch):
    from agent_py_agent.agent.conversation import ConversationStore
    store = ConversationStore(tmp_path / 'conversation')
    agent = SimpleNamespace(conversation_store=store, home_paths=SimpleNamespace(owner_id='local/main'))
    for number in range(8):
        views.renew(agent, 'target-thread', {'view_id': f'view-{number}', 'source_thread_id': 'source',
            'actor_id': 'self', 'conversation_id': 'session'})
    with pytest.raises(ValueError, match='LIMIT'):
        views.renew(agent, 'target-thread', {'view_id': 'ninth-view', 'source_thread_id': 'source',
            'actor_id': 'self', 'conversation_id': 'session'})
    monkeypatch.setattr(views.time, 'time', lambda: 10**12)
    views.renew(agent, 'target-thread', {'view_id': 'new-view', 'source_thread_id': 'source',
        'actor_id': 'self', 'conversation_id': 'session'})
    assert len(views._read(agent, 'target-thread')) == 1


def test_close_serializes_after_original_decision_boundary_then_rejects_late_scope(tmp_path):
    import threading

    from agent_py_agent.agent.conversation import ConversationStore
    store = ConversationStore(tmp_path / 'conversation')
    source = store.threads.get_or_create({'channel': 'chat', 'channel_conversation_id': 'source',
        'channel_user_id': 'self', 'canonical_user_id': 'self', 'owner_id': 'local/main'})
    agent = SimpleNamespace(conversation_store=store, home_paths=SimpleNamespace(owner_id='local/main'))
    binding = {'view_id': 'view-a', 'source_thread_id': source.thread_id, 'actor_id': 'self', 'conversation_id': 'source'}
    views.renew(agent, 'target-thread', binding)
    entered, release, closed = threading.Event(), threading.Event(), threading.Event()

    def decide():
        with views.decision_scope(agent, 'target-thread', binding):
            entered.set()
            assert release.wait(3)

    def leave():
        views.close(agent, 'target-thread', binding)
        closed.set()

    decision = threading.Thread(target=decide)
    leaving = threading.Thread(target=leave)
    decision.start()
    try:
        assert entered.wait(3)
        leaving.start()
        assert not closed.wait(.05)
    finally:
        release.set()
        decision.join(3)
        leaving.join(3)
    assert closed.is_set() and not decision.is_alive() and not leaving.is_alive()
    with pytest.raises(ValueError, match='INACTIVE'):
        with views.decision_scope(agent, 'target-thread', binding):
            pytest.fail('closed interest entered original approval engine')


def test_reenter_same_pending_request_uses_new_interest_ui_key_without_reusing_old_callback():
    from agent_py_agent.cli.chat_parts.tui_runtime import TuiRuntime
    from agent_py_agent.tests.test_gateway_agent_control_service import _child_approval_request
    runtime = TuiRuntime('session-main')
    request = _child_approval_request('child-one')
    rows = [{'run_id': 'child-one', 'request': request.to_dict()}]
    assert runtime.sync_agent_permission_requests(rows, decision_writer=lambda *_: {'ok': False}, interest_id='view-old')
    first = runtime._permission_coordinator.active_controller()
    assert first is not None
    runtime.sync_agent_permission_requests([], decision_writer=lambda *_: {'ok': False}, interest_id='view-old')
    assert runtime._permission_coordinator.active_controller() is None
    assert runtime.sync_agent_permission_requests(rows, decision_writer=lambda *_: {'ok': False}, interest_id='view-new')
    second = runtime._permission_coordinator.active_controller()
    assert second is not None and second is not first


def test_original_root_notice_poll_cannot_erase_selected_background_interest_panel():
    from agent_py_agent.cli.chat_parts.tui_runtime import TuiRuntime
    from agent_py_agent.tests.test_gateway_agent_control_service import _child_approval_request
    runtime = TuiRuntime('session-main')
    background_request = _child_approval_request('background-child')
    foreground_request = _child_approval_request('foreground-child')
    rows = [{'run_id': 'background-child', 'request': background_request.to_dict()}]
    runtime.sync_agent_permission_requests(rows, decision_writer=lambda *_: {'ok': False}, interest_id='selected-view')
    original = runtime._permission_coordinator.active_controller()
    runtime.sync_agent_permission_requests([], decision_writer=lambda *_: {'ok': False})
    assert runtime._permission_coordinator.active_controller() is original
    runtime.sync_agent_permission_requests([{'run_id': 'foreground-child', 'request': foreground_request.to_dict()}],
                                          decision_writer=lambda *_: {'ok': False})
    runtime.sync_agent_permission_requests([], decision_writer=lambda *_: {'ok': False}, interest_id='selected-view')
    assert runtime._permission_coordinator.active_controller() is not None
    assert runtime._permission_coordinator.active_controller() is not original
