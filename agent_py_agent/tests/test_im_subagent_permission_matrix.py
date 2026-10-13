"""三类真实 owner 的创建树、原运行边界、执行器与进程沙箱对照；无线上资料。"""
from pathlib import Path

import pytest

from agent_py_agent.agent.agent_core.tool_runtime_ledger import write_boundary_with_runtime_ledger
from agent_py_agent.agent.core import SimpleAgent
from agent_py_agent.agent.settings import AgentConfig
from agent_py_agent.agent.subagents.context_bundle_refs import runtime_task_attributes
from agent_py_agent.agent.subagents.services.base import CreateRunParams
from agent_py_agent.agent.user_space.owner_resolver import OwnerIdentity, resolve_owner_home
from agent_py_agent.tests._tool_runtime_harness import execute_registry_test_call
from agent_py_agent.tests.test_tool_gateway_contract import _registry
from agent_py_agent.tests.test_web_fetch_ssrf import server as _server_fixture

server = _server_fixture


@pytest.mark.parametrize('role', ['ordinary', 'admin-normal', 'admin-full'])
@pytest.mark.parametrize('enabled', [False, True])
def test_owner_network_switch_reaches_real_child_shell_sandbox(tmp_path, server, role, enabled):
    import json
    import shlex
    from types import SimpleNamespace

    from agent_py_agent.agent.user_space.owner_policy import resolve_effective_owner_policy
    identity = OwnerIdentity.provider_user('feishu', 'ou_net_scope') if role == 'ordinary' else OwnerIdentity.local_main()
    config = AgentConfig(model_backend='echo', my_agent_home=str(tmp_path/'home'), enable_plugins=False,
        my_agent_owner_provider=identity.provider, my_agent_owner_kind=identity.owner_kind, my_agent_owner_id=identity.owner_id,
        access_mode='full-access' if role == 'admin-full' else 'workspace-write')
    owner = resolve_owner_home(tmp_path/'home', identity).home_dir
    agent = SimpleAgent(config, owner)
    path = agent.home_paths.owner_permissions_json
    data = json.loads(path.read_text())
    data['network']['enabled'] = enabled
    path.write_text(json.dumps(data))
    agent.owner_policy = resolve_effective_owner_policy(agent.home_paths)
    agent.subagents.owner_policy_snapshot = agent.owner_policy.to_dict()
    task = None
    for level in range(3):
        task = agent.subagents.create_run(params=CreateRunParams(goal='合成联网探测', thought='', plan=['探测'],
            parent_id=task.id if task else '',depth=level))
    context = agent.subagents.runner_context.build_execution_context(task.id)
    assert task.effective_permissions['network_enabled'] is enabled
    boundary = write_boundary_with_runtime_ledger(agent, SimpleNamespace(write_boundary=context.write_boundary,
        context_scope='task_local',run_id=task.id,task_attributes=runtime_task_attributes(task)))
    directory = Path(task.agent_run_workspace_dir)
    directory.mkdir(parents=True,exist_ok=True)
    registry = _registry(directory)
    registry.owner_scope_root = str(owner)
    program = f'import urllib.request; print(urllib.request.urlopen("http://127.0.0.1:{server.server_address[1]}/page",timeout=1).status)'
    result = execute_registry_test_call(registry,'run_command',{'command':'python3 -c '+shlex.quote(program)},
        run_id=task.id,write_boundary=boundary)
    assert result.ok is enabled, (result.error_code,result.output)
    assert result.handler_executed
    assert bool(server.request_paths) is enabled


@pytest.mark.parametrize('role', ['ordinary', 'admin-normal', 'admin-full'])
@pytest.mark.parametrize('explicit', [None, True, False])
def test_same_owner_model_network_setting_and_api_auth_agree(tmp_path, monkeypatch, server, role, explicit):
    identity = OwnerIdentity.provider_user('feishu', 'ou_network_test') if role == 'ordinary' else OwnerIdentity.local_main()
    config = AgentConfig(model_backend='echo', my_agent_home=str(tmp_path/'home'), enable_plugins=False,
        my_agent_owner_provider=identity.provider, my_agent_owner_kind=identity.owner_kind, my_agent_owner_id=identity.owner_id,
        access_mode='full-access' if role == 'admin-full' else 'workspace-write')
    agent = SimpleAgent(config, resolve_owner_home(tmp_path/'home', identity).home_dir)
    monkeypatch.setenv('MY_AGENT_ALLOW_PRIVATE_URLS', 'true')
    boundary = {} if explicit is None else {'allow_private_resolution': explicit}
    result = execute_registry_test_call(agent.tools, 'web_fetch', {
        'url': f'http://127.0.0.1:{server.server_address[1]}/page',
        'headers': {'Authorization': 'Bearer synthetic-test-only'}, 'mode': 'text',
    }, write_boundary=boundary, register_with=agent)
    assert result.ok is (explicit is not False), (role, explicit, result.error_code, result.output)
    if explicit is not False:
        assert 'Hello world' in result.output
        assert server.request_headers[-1]['Authorization'] == 'Bearer synthetic-test-only'
    else:
        assert not server.request_paths
    assert agent.owner_policy.network_enabled is True
    assert agent.config.model_backend == 'echo'


@pytest.mark.parametrize('role', ['ordinary', 'admin-normal', 'admin-full'])
def test_relative_capability_grant_matches_child_execution_cwd_not_host_cwd(tmp_path, role):
    from agent_py_agent.agent.agent_core.orchestration.tools.capability import (
        ResolveCapabilityRequestsTool,
    )
    from agent_py_agent.agent.subagents.services.lifecycle import RecordCapabilityRequestParams
    identity = OwnerIdentity.provider_user('feishu', 'ou_grant_test') if role == 'ordinary' else OwnerIdentity.local_main()
    config = AgentConfig(model_backend='echo', my_agent_home=str(tmp_path/'home'), enable_plugins=False,
        my_agent_owner_provider=identity.provider, my_agent_owner_kind=identity.owner_kind, my_agent_owner_id=identity.owner_id,
        access_mode='full-access' if role == 'admin-full' else 'workspace-write')
    owner = resolve_owner_home(tmp_path/'home', identity).home_dir
    agent = SimpleAgent(config, owner)
    run_root = owner/'runs/2026-10-13/grant-main'
    task = agent.subagents.create_run(params=CreateRunParams(goal='合成授权测试', thought='', plan=['写结果'],
        allowed_tools=['capability_request'], attributes={
            'workspace_root': str(owner),
            'run_workspace': {'task_root': str(run_root), 'work_dir': str(run_root/'work'), 'output_dir': str(run_root/'output')},
        }))
    business = owner/'tasks/grant-business'
    business.mkdir()
    agent.subagents.lifecycle.record_capability_request(task.id, RecordCapabilityRequestParams(
        problem='合成写权限', needed_capability='写测试文件', expected_output='测试结果',
        capability_type='tool', requested_tools=['write_file','run_command'], path_scope=['tasks/grant-business']))
    result = ResolveCapabilityRequestsTool(agent).execute({'run_id':task.id,'decision':'grant','reason':'合成授权'})
    assert result.ok, result.output
    reloaded = agent.subagents.load(task.id)
    assert reloaded.capability_grants[0].path_scope == [str(business)]
    context = agent.subagents.runner_context.build_execution_context(task.id)
    assert str(business) in context.write_boundary['capability_write_roots']
    from types import SimpleNamespace
    boundary = write_boundary_with_runtime_ledger(agent, SimpleNamespace(write_boundary=context.write_boundary,
        context_scope='task_local',run_id=task.id,task_attributes=runtime_task_attributes(reloaded)))
    registry = _registry(owner)
    registry.owner_scope_root = str(owner)
    result = execute_registry_test_call(registry,'write_file',{'path':str(business/'result.txt'),'content':'grant works'},
        run_id=task.id,write_boundary=boundary)
    assert result.ok,result.output
    assert (business/'result.txt').read_text()=='grant works'


@pytest.mark.parametrize('role',['ordinary','admin-normal','admin-full'])
@pytest.mark.parametrize('depth',[0,1,2])
def test_real_created_tree_own_home_file_and_shell_agree_and_cannot_cross_owner(tmp_path,role,depth):
    import shlex
    from types import SimpleNamespace

    identity=OwnerIdentity.provider_user('feishu','ou_test') if role=='ordinary' else OwnerIdentity.local_main()
    config=AgentConfig(model_backend='echo',my_agent_home=str(tmp_path/'home'),enable_plugins=False,
        my_agent_owner_provider=identity.provider,my_agent_owner_kind=identity.owner_kind,my_agent_owner_id=identity.owner_id,
        access_mode='full-access' if role=='admin-full' else 'workspace-write')
    agent=SimpleAgent(config,resolve_owner_home(tmp_path/'home',identity).home_dir)
    business=agent.home_paths.owner_tasks_dir/'synthetic-business'
    business.mkdir()
    task=None
    root_id=''
    run_root=agent.home_paths.owner_runs_dir/'2026-10-13'/'synthetic-main'
    for level in range(depth+1):
        task=agent.subagents.create_run(params=CreateRunParams(goal='合成文件核对',thought='',plan=['写合成结果'],
            parent_id=task.id if task else '',root_id=root_id,depth=level,extra_write_roots=[str(business)],
            attributes={'run_workspace':{'task_root':str(run_root),'work_dir':str(run_root/'work'),'output_dir':str(run_root/'output')}},
            parent_access_mode=config.access_mode if level==0 else 'workspace-write'))
        root_id=root_id or task.id
    context=agent.subagents.runner_context.build_execution_context(task.id)
    assert context.effective_permissions['shell_access_mode']=='workspace-write'
    params=SimpleNamespace(write_boundary=context.write_boundary,context_scope='task_local',run_id=task.id,
        task_attributes=runtime_task_attributes(task))
    boundary=write_boundary_with_runtime_ledger(agent,params)
    own=Path(task.agent_run_workspace_dir)
    own.mkdir(parents=True,exist_ok=True)
    owner=agent.home_paths.owner_home_dir
    registry=_registry(owner)
    registry.owner_scope_root=str(owner)
    def call(name,arguments):
        return execute_registry_test_call(registry,name,arguments,run_id=task.id,write_boundary=boundary)
    for directory in (own,business):
        target=directory/'same-file.txt'
        written=call('write_file',{'path':str(target),'content':'file'})
        assert written.ok,(written.error_code,written.output)
        shell=call('run_command',{'command':'printf shell > '+shlex.quote(str(target))})
        assert shell.ok,shell.output
        assert target.read_text()=='shell'
        assert call('read_file',{'path':str(target)}).ok
    nested=business/'nested'/'ordinary.txt'
    result=call('run_command',{'command':'mkdir -p '+shlex.quote(str(nested.parent))+' && printf nested > '+shlex.quote(str(nested))})
    assert result.ok,result.output
    assert nested.read_text()=='nested'
    foreign=resolve_owner_home(agent.home_paths.root,OwnerIdentity.provider_user('feishu','ou_foreign')).home_dir
    foreign.mkdir(parents=True)
    target=foreign/'private.txt'
    target.write_text('foreign data')
    for destination in (target,agent.home_paths.owner_permissions_json):
        original=destination.read_text()
        assert not call('write_file',{'path':str(destination),'content':'changed'}).ok
        assert not call('run_command',{'command':'printf changed > '+shlex.quote(str(destination))}).ok
        assert destination.read_text()==original
    assert not call('read_file',{'path':str(target)}).ok
