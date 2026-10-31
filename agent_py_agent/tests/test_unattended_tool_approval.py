"""无人值守审批：部署默认、精确停止、绝对期限和假模型重复调用的原执行链。"""
from dataclasses import replace
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.contracts.tool_approval import (
    ToolApprovalDecision,
    ToolApprovalWaitOptions,
)
from agent_py_agent.agent.conversation import agent_tool_approval as bridge
from agent_py_agent.agent.gateway_parts import permission_bridge
from agent_py_agent.agent.gateway_parts.stream_approval import (
    StreamApproval,
    StreamApprovalRequestOptions,
)
from agent_py_agent.agent.settings.config import AgentConfig
from agent_py_agent.agent.user_space.approval_mode import (
    ApprovalModeError,
    deployment_approval_config,
    permission_config,
    read_permission_state,
)
from agent_py_agent.tests.test_background_tool_approval import background
from agent_py_agent.tests.test_owner_approval_mode import owner_home

_FIXTURES = (background,)


def test_missing_owner_mode_uses_shared_deployment_and_does_not_write_owner(tmp_path):
    home = owner_home(tmp_path)
    home.root.joinpath('config').mkdir(parents=True)
    config_path = home.root / 'config/capability_config.yaml'
    config_path.write_text('owner_default_permission_mode: ask\ntool_approval_timeout_seconds: 37\n')
    state = read_permission_state(home)
    assert state['mode'] == 'ask' and state['source'] == 'deployment'
    assert deployment_approval_config(home).tool_approval_timeout_seconds == 37
    assert not home.owner_tool_policy_json.exists()
    config_path.write_text('owner_default_permission_mode: auto\n')
    assert read_permission_state(home)['mode'] == 'auto'
    legacy = AgentConfig(access_mode='full-access', path_access_mode='full')
    assert permission_config(legacy, owner_home(tmp_path, admin=True)) is legacy
    assert permission_config(legacy, owner_home(tmp_path, admin=True), inherited=True).access_mode == 'workspace-write'


@pytest.mark.parametrize('payload', ['{}', '[]', '{broken', '{"schema_version":"bad"}'])
def test_invalid_owner_policy_is_not_converted_to_default_auto(tmp_path, payload):
    home = owner_home(tmp_path)
    home.owner_tool_policy_json.parent.mkdir(parents=True)
    home.owner_tool_policy_json.write_text(payload)
    with pytest.raises(ApprovalModeError):
        read_permission_state(home)
    assert home.owner_tool_policy_json.read_text() == payload


@pytest.mark.parametrize('value', ['full-access', 'unknown', 'false'])
def test_invalid_shared_default_rejects_without_owner_write(tmp_path, value):
    home = owner_home(tmp_path)
    path = home.root / 'config/capability_config.yaml'
    path.parent.mkdir(parents=True)
    path.write_text(f'owner_default_permission_mode: {value}\n')
    with pytest.raises(ApprovalModeError):
        read_permission_state(home)
    assert not home.owner_tool_policy_json.exists()


def test_default_background_rejects_before_publication_even_with_live_consumer(background, monkeypatch):
    ctx = background
    ctx.sink._transcript.approval_interactive = False
    bridge.renew_agent_tool_approval_consumer(ctx.agent, root_task_id=ctx.task_id)
    monkeypatch.setattr(bridge, 'publish_agent_tool_approval', lambda *_a, **_k: pytest.fail('后台不能发布 pending'))
    response = ctx.sink.request_permission(ctx.request.to_dict())
    assert response['decision'] == 'denied' and response['reason_code'] == 'APPROVAL_NONINTERACTIVE'
    assert not bridge.list_pending_agent_tool_approvals(ctx.agent, root_task_id=ctx.task_id)


def test_agent_deadline_wins_over_continuous_consumer_lease(background, monkeypatch):
    ctx = background
    handle = bridge.publish_agent_tool_approval(ctx.agent, run_id=ctx.task_id,
        thread_id=ctx.thread.thread_id, request_value=ctx.request)
    handle = replace(handle, scope_valid=lambda: True)
    deadline = ctx.request.requested_at + 1800
    now = [deadline - 1]
    monkeypatch.setattr(bridge.time, 'time', lambda: now[0])
    monkeypatch.setattr(bridge, '_consumer_seen_at', lambda *_args: now[0])
    monkeypatch.setattr(bridge.time, 'sleep', lambda _interval: now.__setitem__(0, deadline))
    decision = bridge.wait_for_agent_tool_approval(handle)
    assert decision.reason_code == 'APPROVAL_TIMEOUT' and decision.decision == 'denied'
    assert not handle.path.exists()
    with pytest.raises(FileNotFoundError):
        bridge.resolve_agent_tool_approval(ctx.agent, run_id=ctx.task_id, request_value=ctx.request,
            decision_value=ToolApprovalDecision(ctx.request.permission_id, 'approved'))


def test_configured_longer_deadline_is_not_capped_by_dataclass_default(background, monkeypatch):
    ctx = background
    path = ctx.agent.home_paths.root / 'config/capability_config.yaml'
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('tool_approval_timeout_seconds: 3600\n')
    handle = bridge.publish_agent_tool_approval(ctx.agent, run_id=ctx.task_id,
        thread_id=ctx.thread.thread_id, request_value=ctx.request)
    handle = replace(handle, scope_valid=lambda: True)
    monkeypatch.setattr(bridge.time, 'time', lambda: ctx.request.requested_at + 2000)
    decision = bridge.wait_for_agent_tool_approval(handle, options=ToolApprovalWaitOptions(
        mode_decision_provider=lambda request: ToolApprovalDecision(request.permission_id, 'approved')))
    assert decision.approved and not handle.path.exists()


def test_stream_timeout_is_absolute_and_late_decision_is_rejected(background, tmp_path, monkeypatch):
    request = replace(background.request, expires_at=background.request.requested_at + 10)
    now = [request.expires_at - 1]
    monkeypatch.setattr(permission_bridge.time, 'time', lambda: now[0])
    monkeypatch.setattr(permission_bridge.time, 'sleep', lambda _seconds: now.__setitem__(0, request.expires_at))
    decision = permission_bridge.wait_for_gateway_permission_decision(tmp_path/'chunks', request)
    assert decision.reason_code == 'APPROVAL_TIMEOUT'
    with pytest.raises(ValueError, match='expired'):
        permission_bridge.write_gateway_permission_decision(tmp_path/'chunks', request,
            ToolApprovalDecision(request.permission_id, 'approved'))


def test_noninteractive_stream_rejects_without_publish_and_auto_preserves_plugin_gate(background, tmp_path):
    request = background.request
    stream = StreamApproval(tmp_path/'chunks', lambda _value: pytest.fail('不能发布待审批'), lambda: pytest.fail('不能等待'))
    options = StreamApprovalRequestOptions(interactive=False)
    assert stream.request(request.to_dict(), options=options)['reason_code'] == 'APPROVAL_NONINTERACTIVE'
    automatic = replace(options, mode_decision_provider=lambda req: ToolApprovalDecision(req.permission_id, 'approved'))
    assert stream.request(request.to_dict(), options=automatic)['decision'] == 'approved'
    plugin = replace(request, binding={**request.binding, 'plugin_gate_ref': 'host-signed-gate'})
    assert stream.request(plugin.to_dict(), options=automatic)['reason_code'] == 'APPROVAL_NONINTERACTIVE'
    cancelled = replace(automatic, cancellation_token=SimpleNamespace(cancelled=True))
    assert stream.request(request.to_dict(), options=cancelled)['decision'] == 'cancelled'


@pytest.mark.parametrize('decision,reason', [('unavailable', ''), ('denied', 'APPROVAL_TIMEOUT'),
    ('denied', 'APPROVAL_NONINTERACTIVE'), ('unavailable', 'APPROVAL_TRANSPORT_FAILED')])
def test_fake_model_new_call_cannot_reask_same_denied_operation(decision, reason):
    from agent_py_agent.agent.agent_core.tool_loop.round_execution import execute_tool_round
    from agent_py_agent.agent.backends import ModelResponse
    from agent_py_agent.tests.test_tool_round_execution import _approval_pending, _round_request

    prompts, results = [], []
    def consume(payload, **_kwargs):
        prompts.append(payload)
        return ToolApprovalDecision(payload['permission_id'], decision, reason_code=reason)
    params = SimpleNamespace(tool_context=[], request_id='fake-model-request', cancellation_token=None,
        effective_on_chunk=SimpleNamespace(request_permission=consume), runtime_rejected_actions=[], runtime_approved_actions=[])
    # 两次假模型回合生成不同 call/attempt，原工具执行器只能记未执行拒绝。
    calls = []
    for number in (1, 2):
        round_request = _round_request(agent=SimpleNamespace(), params=params, tool_rounds=number,
            response=ModelResponse(text='', backend='fake-model'),
            calls=[{'tool':'terminal_session','action':'start','command':'sh'}],
            execute_one=_approval_pending, record_one=lambda record: results.append(record.result))
        round_request = replace(round_request, calls=[replace(round_request.calls[0],
            call_id=f'fake-{number}', attempt_id=f'attempt-{number}', operation_id=f'operation-{number}')])
        calls.append(round_request.calls[0])
        execute_tool_round(round_request)
    assert calls[0].call_id != calls[1].call_id and len(prompts) == 1
    assert len(params.runtime_rejected_actions) == 1
    assert all(not result.handler_executed and result.error_code == (reason or 'APPROVAL_UNAVAILABLE') for result in results), [(result.error_code, result.metadata) for result in results]
    assert all(result.retryable is False for result in results)


@pytest.mark.parametrize('mode', ['ask', 'auto'])
@pytest.mark.parametrize('tool_name,action', [('process_session','stop'), ('terminal_session','close'), ('cancel_subagents',None)])
def test_owned_stop_actions_do_not_enter_optional_confirmation(tmp_path, mode, tool_name, action):
    from agent_py_agent.agent.agent_core.orchestration.tools.cancel import CancelSubagentsTool
    from agent_py_agent.agent.tooling.action_policy import ActionPolicy, ActionPolicyRequest
    from agent_py_agent.agent.tooling.process_sessions import ProcessSessionTool
    from agent_py_agent.agent.tooling.pty_sessions import TerminalSessionTool
    from agent_py_agent.tests._tool_runtime_harness import (
        canonical_test_call,
        runtime_snapshot_for_tools,
    )

    definition = {'process_session':ProcessSessionTool, 'terminal_session':TerminalSessionTool,
                  'cancel_subagents':CancelSubagentsTool}[tool_name]
    tool = SimpleNamespace(model_spec=definition.model_spec, runtime_policy=definition.runtime_policy)
    snapshot = runtime_snapshot_for_tools({tool_name:tool})
    args = {'action':action, 'session_id':'owned-session'} if action else {'run_id':'direct-child'}
    decision = ActionPolicy().decide(ActionPolicyRequest(canonical_test_call(snapshot,tool_name,args),
        snapshot, tmp_path, approval_mode=mode))
    assert decision.status == 'allow', decision.to_dict()


@pytest.mark.parametrize('bad_deadline', ['broken', float('inf'), float('nan'), -1])
def test_corrupt_pending_deadline_fails_closed_and_reclaims_exact_record(background, monkeypatch, bad_deadline):
    import json
    ctx = background
    handle = bridge.publish_agent_tool_approval(ctx.agent, run_id=ctx.task_id,
        thread_id=ctx.thread.thread_id, request_value=ctx.request)
    row = json.loads(handle.path.read_text())
    row['expires_at'] = bad_deadline
    handle.path.write_text(json.dumps(row))
    decision = bridge.wait_for_agent_tool_approval(handle)
    assert decision.decision == 'unavailable' and decision.reason_code == 'APPROVAL_UNAVAILABLE'
    assert not handle.path.exists()


def test_typed_waiting_input_stops_same_round_following_tools():
    from agent_py_agent.agent.agent_core.tool_loop.round_execution import execute_tool_round
    from agent_py_agent.agent.backends import ModelResponse
    from agent_py_agent.tests.test_tool_round_execution import _round_request, _success
    params = SimpleNamespace(tool_context=[], live_archive_state={})
    calls, results = [], []
    def execute(request):
        calls.append(request.call.tool_name)
        return _success(request, '已发布输入依赖', result_envelope={
            'input_dependency':{'status':'waiting_input','credential_request_id':'synthetic-dependency'}})
    execute_tool_round(_round_request(agent=SimpleNamespace(), params=params, tool_rounds=1,
        response=ModelResponse(text='', backend='fake-model'),
        calls=[{'tool':'request_credentials','action':'request'}, {'tool':'run_command','command':'true'}],
        execute_one=execute, record_one=lambda record: results.append(record.result)))
    assert calls == ['request_credentials']
    assert len(results) == 2 and results[1].error_code == 'RUNTIME_TRANSITION_DEFERRED'
    assert not results[1].handler_executed
    assert params.live_archive_state['pending_runtime_transition']['kind'] == 'input_wait'


def test_late_consumer_approval_cannot_start_original_tool(monkeypatch):
    from agent_py_agent.agent.agent_core.tool_loop import round_execution
    from agent_py_agent.agent.agent_core.tool_loop.round_execution import execute_tool_round
    from agent_py_agent.agent.backends import ModelResponse
    from agent_py_agent.tests.test_tool_round_execution import _approval_pending, _round_request
    prompts, results = [], []
    def consume(payload, **_kwargs):
        prompts.append(payload)
        monkeypatch.setattr(round_execution.time, 'time', lambda: payload['requested_at'] + 1801)
        return {'permission_id':payload['permission_id'], 'decision':'approved'}
    params = SimpleNamespace(tool_context=[], request_id='late-request', cancellation_token=None,
        effective_on_chunk=SimpleNamespace(request_permission=consume), runtime_rejected_actions=[], runtime_approved_actions=[])
    execute_tool_round(_round_request(agent=SimpleNamespace(), params=params, tool_rounds=1,
        response=ModelResponse(text='',backend='fake-model'), calls=[{'tool':'run_command','command':'true'}],
        execute_one=_approval_pending, record_one=lambda record: results.append(record.result)))
    assert len(prompts) == 1 and results[0].error_code == 'APPROVAL_TIMEOUT'
    assert not results[0].handler_executed and not params.runtime_approved_actions


@pytest.mark.parametrize('key,expected', [('owner_default_permission_mode','ask'),('tool_approval_timeout_seconds','37')])
def test_deployment_parameter_projection_ignores_owner_local_and_keeps_writes_blocked(tmp_path, key, expected):
    import json

    from agent_py_agent.agent.settings.parameter_changes import (
        ChangeOrigin,
        WritePaths,
        set_parameter,
        user_settings_write_scope,
    )
    from agent_py_agent.agent.settings.parameter_registry import parameter_registry, running_value
    from agent_py_agent.agent.tooling.user_config_tool import UserConfigTool
    host = tmp_path / 'host'
    shared = host / 'config/capability_config.yaml'
    shared.parent.mkdir(parents=True)
    shared.write_text('owner_default_permission_mode: ask\ntool_approval_timeout_seconds: 37\n')
    local = host / 'owners/local/main/workspace/config/capability_config.yaml'
    local.parent.mkdir(parents=True)
    local.write_text('owner_default_permission_mode: auto\ntool_approval_timeout_seconds: 999\n')
    config = AgentConfig(my_agent_home=str(host))
    spec = parameter_registry()[key]
    assert str(running_value(spec, config, capability_path=local)) == expected
    agent = SimpleNamespace(config=config, capability_config_path=local, root=local.parent,
        home_paths=owner_home(host, admin=True))
    payload = json.loads(UserConfigTool(agent).execute({'action':'view','key':key}).output)
    assert payload['parameter']['running_value'] == payload['fact']['effective'] == expected
    assert payload['parameter']['source'] == 'deployment_capability'
    assert payload['fact']['source'] == 'deployment_config' and payload['fact']['source_scope'] == 'deployment'
    assert payload['parameter']['writable'] is False and payload['parameter']['source_readonly']
    original = shared.read_bytes(), local.read_bytes()
    for scope in (False, True):
        if scope:
            with user_settings_write_scope():
                result = set_parameter(key,'auto' if key == 'owner_default_permission_mode' else '100',
                    paths=WritePaths(None,local),origin=ChangeOrigin('test'))
        else:
            result = set_parameter(key,'auto' if key == 'owner_default_permission_mode' else '100',
                paths=WritePaths(None,local),origin=ChangeOrigin('test'))
        # 宿主部署层先于安全边界拒绝（a12fe40c5），任何作用域都不写 owner 局部覆盖。
        assert not result['ok'] and result['code'] == 'PARAMETER_SOURCE_READ_ONLY'
    assert (shared.read_bytes(),local.read_bytes()) == original
    assert running_value(spec,None,capability_path=local) == 'unknown'
    shared.write_text('owner_default_permission_mode: broken\n')
    assert running_value(spec,config,capability_path=local) == 'unknown'


@pytest.mark.parametrize('actual_exists', [True, False])
@pytest.mark.parametrize('key', ['feishu_cli_backend_enabled', 'feishu_user_oauth_enabled'])
@pytest.mark.parametrize('configured_root', ['~/.my-agent', ''])
def test_deployment_projection_uses_runtime_env_home_for_default(tmp_path, monkeypatch, key, actual_exists, configured_root):
    from agent_py_agent.agent.capability.runtime_config_reload import deployment_capability_config
    from agent_py_agent.agent.settings.parameter_registry import parameter_registry, running_value
    from agent_py_agent.agent.settings.user_config_capability import read_config_fact
    from agent_py_agent.agent.user_space.home_root import configured_home_root
    fake, actual = tmp_path / 'fake-home', tmp_path / 'actual-home'
    monkeypatch.setenv('HOME', str(fake))
    monkeypatch.setenv('MY_AGENT_HOME', str(actual))
    fake_config = fake / '.my-agent/config/capability_config.yaml'
    fake_config.parent.mkdir(parents=True)
    fake_config.write_text(f'{key}: false\n')
    if actual_exists:
        actual_config = actual / 'config/capability_config.yaml'
        actual_config.parent.mkdir(parents=True)
        actual_config.write_text(f'{key}: true\n')
    config = AgentConfig(my_agent_home=configured_root)
    assert configured_home_root(config) == str(actual)
    runtime = deployment_capability_config(SimpleNamespace(root=actual))
    spec = parameter_registry()[key]
    assert running_value(spec, config, capability_path=fake_config) == getattr(runtime, key)
    fact = read_config_fact(key, config=config, user_path=fake_config, default_path=fake_config)
    assert fact['effective'] == str(getattr(runtime, key)).lower()
    assert fact['source'] == ('deployment_config' if actual_exists else 'packaged_default')
    assert fact['source_readonly'] is True
    assert fake_config.read_text() == f'{key}: false\n'


def test_deployment_projection_rejects_relative_env_home_and_keeps_explicit_home(tmp_path, monkeypatch):
    from agent_py_agent.agent.settings.parameter_registry import parameter_registry, running_value
    from agent_py_agent.agent.settings.user_config_capability import read_config_fact
    monkeypatch.setenv('MY_AGENT_HOME', 'relative-host')
    spec = parameter_registry()['tool_approval_timeout_seconds']
    default_config = AgentConfig(my_agent_home='~/.my-agent')
    assert running_value(spec, default_config) == 'unknown'
    assert read_config_fact(spec.key, config=default_config, user_path=None, default_path=tmp_path/'unused.yaml')['source'] == 'unknown'
    shared = tmp_path / 'explicit/config/capability_config.yaml'
    shared.parent.mkdir(parents=True)
    shared.write_text('tool_approval_timeout_seconds: 37\n')
    explicit_config = AgentConfig(my_agent_home=str(shared.parent.parent))
    assert running_value(spec, explicit_config) == 37
    fact = read_config_fact(spec.key, config=explicit_config, user_path=None, default_path=tmp_path/'unused.yaml')
    assert fact['effective'] == '37' and fact['source'] == 'deployment_config'


def test_production_plugin_context_injects_shared_deadline_into_host_command(tmp_path, monkeypatch):
    from agent_py_agent.agent.plugin_management import PluginManagement, plugin_management_context
    from agent_py_agent.agent.tooling.models import ApprovalPolicy
    from agent_py_agent.agent.user_space.home_layout import home_paths
    from agent_py_agent.agent.user_space.owner_resolver import home_paths_with_owner
    from agent_py_agent.tests.test_plugin_management import manager
    service, source = manager(tmp_path)
    owner = service.context.owner
    home = home_paths_with_owner(home_paths(tmp_path/'home'),owner)
    shared = home.root/'config/capability_config.yaml'
    shared.parent.mkdir(parents=True)
    shared.write_text('owner_default_permission_mode: ask\ntool_approval_timeout_seconds: 37\n')
    local = owner.home_dir/'config/capability_config.yaml'
    local.parent.mkdir(parents=True)
    local.write_text('tool_approval_timeout_seconds: 999\n')
    config = AgentConfig(my_agent_home=str(home.root),access_mode='full-access',path_access_mode='full')
    context = plugin_management_context(owner,home,config,service.context.threads,
        actor_id='tester',channel='chat',conversation_id='session',is_admin=True)
    assert context.approval_timeout_seconds == 37
    selected = PluginManagement(context)
    original = selected._management_tool
    def controlled_tool(*args):
        tool = original(*args)
        tool.runtime_policy = replace(tool.runtime_policy,approval_policy=ApprovalPolicy('always'))
        return tool
    monkeypatch.setattr(selected,'_management_tool',controlled_tool)
    prompts=[]
    def deny(value,**_kwargs):
        prompts.append(value)
        return ToolApprovalDecision(value['permission_id'],'denied')
    result = selected.command(f'/plugins install "{source}"',revision=selected.catalog().revision,
        request_id='shared-deadline',request_permission=deny)
    assert result['state'] == 'rejected' and result['error_code'] == 'APPROVAL_REJECTED'
    assert len(prompts) == 1 and prompts[0]['expires_at']-prompts[0]['requested_at'] == 37
    assert not selected.installations.snapshot()


@pytest.mark.parametrize('seconds', [0,-1,True,'37'])
def test_invalid_host_command_timeout_is_terminal_without_consumer_or_handler(tmp_path, seconds):
    from agent_py_agent.agent.runtime_db.host_command_execution import execute_host_command
    from agent_py_agent.tests.test_host_command_approval import approval_case
    repo,request,tool,prepare = approval_case(tmp_path)
    result=execute_host_command(repo,request,prepare,approval_timeout_seconds=seconds,
        request_permission=lambda *_a,**_k: pytest.fail('非法deadline不能发布等待'))
    assert result['state']=='rejected' and result['error_code']=='APPROVAL_UNAVAILABLE' and tool.calls==0
