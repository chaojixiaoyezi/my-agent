"""受管业务单默认按原任务隔离；显式共享根及普通会话合同保持。"""
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.agent_core.run_task_workspace_writer import (
    _now_iso,
    _primary_workspace_root,
)
from agent_py_agent.agent.agent_core.tool_runtime_ledger import write_boundary_with_runtime_ledger
from agent_py_agent.agent.conversation.business_contracts import BusinessExecutionContext
from agent_py_agent.tests.test_tool_gateway_contract import _execute, _registry


def case(tmp_path, task='order-a', space='space-a', *, explicit=None, source='background_main_agent', child=False):
    owner = tmp_path / 'owners/local/main'
    day = _now_iso()[:10]
    work = owner / 'tasks' / day / task / 'work'
    output = work.parent / 'output'
    internal = owner / 'runs' / day / task
    for path in (work, output, internal / 'work', internal / 'output'):
        path.mkdir(parents=True, exist_ok=True)
    registry = _registry(owner)
    registry.owner_scope_root = str(owner)
    home = SimpleNamespace(owner_home_dir=owner, owner_runs_dir=owner/'runs', owner_tasks_dir=owner/'tasks')
    agent = SimpleNamespace(root=owner, tools=registry, home_paths=home,
        subagents=SimpleNamespace(owner_scope_root=str(owner)),
        config=SimpleNamespace(access_mode='workspace-write'))
    attrs = {'conversation_thread_id': 'thread-'+task, 'conversation_task_id': task,
        'conversation_session_task_id': task, 'conversation_execution_cwd': str(explicit or owner),
        'conversation_runtime_workspace_roots': [str(explicit or owner)],
        'conversation_execution_workspace_explicit': explicit is not None,
        'run_workspace': {'task_root':str(internal), 'work_dir':str(internal/'work'), 'output_dir':str(output)}}
    params = SimpleNamespace(context_scope='task_local' if child else 'default', source=source, run_id='',
        task_id=task, business_context=BusinessExecutionContext('local/main',space,None,task,1,None,(),(),'business-context.v1'),
        task_attributes=attrs, delivery_contract=None,
        write_boundary={'allowed_write_roots':[str(work),str(output)]} if child else {})
    return SimpleNamespace(agent=agent, params=params, registry=registry, owner=owner, work=work, output=output)


@pytest.mark.parametrize('other_space',['space-a','space-b'])
def test_default_managed_order_cannot_write_another_order(tmp_path, other_space):
    one=case(tmp_path)
    two=case(tmp_path,'order-b',other_space)
    boundary=write_boundary_with_runtime_ledger(one.agent,one.params)
    attempted=_execute(one.registry,'write_file',{'path':str(two.output/'foreign.txt'),'content':'foreign'},write_boundary=boundary)
    assert not attempted.ok, attempted.output
    assert not (two.output/'foreign.txt').exists()
    own=_execute(one.registry,'write_file',{'path':'own.txt','content':'own'},write_boundary=boundary)
    assert own.ok, own.output
    assert (one.work/'own.txt').read_text()=='own'
    assert _primary_workspace_root(one.agent,one.params)==one.work


@pytest.mark.parametrize('explicit_kind', ['project', 'owner-shared'])
def test_explicit_workspace_keeps_original_shared_authority(tmp_path, explicit_kind):
    one = case(tmp_path)
    explicit = one.owner / 'project' if explicit_kind == 'project' else one.owner
    explicit.mkdir(parents=True, exist_ok=True)
    one = case(tmp_path, explicit=explicit)
    other = case(tmp_path, 'order-b')
    boundary = write_boundary_with_runtime_ledger(one.agent, one.params)
    assert boundary['execution_cwd'] == str(explicit)
    assert _primary_workspace_root(one.agent, one.params) == explicit
    result = _execute(one.registry, 'write_file', {'path': str(other.output/'shared.txt'), 'content':'allowed'}, write_boundary=boundary)
    assert result.ok, result.output


def test_managed_child_keeps_frozen_parent_roots_without_owner_expansion(tmp_path):
    one = case(tmp_path, child=True)
    other = case(tmp_path, 'order-b')
    boundary = write_boundary_with_runtime_ledger(one.agent, one.params)
    assert str(one.owner) not in boundary['allowed_write_roots']
    own = _execute(one.registry, 'write_file', {'path': str(one.output/'child.txt'), 'content':'own'}, write_boundary=boundary)
    assert own.ok, own.output
    foreign = _execute(one.registry, 'write_file', {'path': str(other.output/'no.txt'), 'content':'foreign'}, write_boundary=boundary)
    assert not foreign.ok and not (other.output/'no.txt').exists()


def test_configured_shared_root_is_retained_for_default_order(tmp_path):
    one = case(tmp_path)
    shared = one.owner/'shared'
    shared.mkdir()
    one.agent.config.additional_write_roots = [str(shared)]
    boundary = write_boundary_with_runtime_ledger(one.agent, one.params)
    result = _execute(one.registry, 'write_file', {'path':str(shared/'yes.txt'), 'content':'shared'}, write_boundary=boundary)
    assert result.ok, result.output


def test_ordinary_frontend_keeps_original_owner_paths(tmp_path):
    one = case(tmp_path, source='gateway')
    other = case(tmp_path, 'order-b')
    boundary = write_boundary_with_runtime_ledger(one.agent, one.params)
    assert boundary['execution_cwd'] == str(one.owner)
    result = _execute(one.registry, 'write_file', {'path':str(other.output/'ordinary.txt'), 'content':'allowed'}, write_boundary=boundary)
    assert result.ok, result.output


def test_host_distinguishes_explicit_owner_directory_from_default(tmp_path):
    from agent_py_agent.agent.conversation.runtime import (
        _apply_background_thread_workspace_attributes,
    )

    one = case(tmp_path)
    for declared in ['', str(one.owner)]:
        attrs = {'conversation_thread_id':'thread-a'}
        thread = SimpleNamespace(cwd=declared, runtime_workspace_roots=())
        _apply_background_thread_workspace_attributes(attrs, thread=thread, agent=one.agent)
        assert attrs['conversation_execution_workspace_explicit'] is bool(declared)
        assert attrs['conversation_execution_cwd'] == str(one.owner)


def test_materialized_default_order_is_inherited_by_original_child_policy(tmp_path):
    from agent_py_agent.agent.agent_core.orchestration.create_constraints import (
        delegated_product_write_roots,
    )
    from agent_py_agent.agent.agent_core.run_task_workspace_writer import (
        attach_run_task_workspace_context,
    )
    agent, params = real_case(tmp_path)
    params = attach_run_task_workspace_context(agent, params, '完成测试任务')
    agent._current_run_params = params
    roots = delegated_product_write_roots(agent)
    expected_work, expected_output = (Path(params.task_attributes['conversation_execution_cwd']),
        Path(params.delivery_contract['task_workspace']['output_dir']))
    assert set(roots) == {str(expected_work), str(expected_output)}
    assert expected_work.is_dir() and expected_output.is_dir()
    boundary = write_boundary_with_runtime_ledger(agent, params)
    assert set(boundary['allowed_write_roots']) == set(roots)
    assert boundary['execution_cwd'] == str(expected_work)


def test_next_order_ignores_previous_thread_run_workspace(tmp_path):
    one = case(tmp_path)
    old = case(tmp_path, 'previous-order')
    one.params.task_attributes['run_workspace']['output_dir'] = str(old.output)
    one.params.delivery_contract = {'task_workspace': {'output_dir': str(old.output)}}
    boundary = write_boundary_with_runtime_ledger(one.agent, one.params)
    assert boundary['execution_cwd'] == str(one.work)
    denied = _execute(one.registry, 'write_file', {'path':str(old.output/'stale.txt'), 'content':'no'}, write_boundary=boundary)
    assert not denied.ok and not (old.output/'stale.txt').exists()


def test_managed_child_retains_explicit_shared_parent_grant(tmp_path):
    one = case(tmp_path, child=True)
    shared = one.owner/'shared'
    shared.mkdir()
    one.params.write_boundary['allowed_write_roots'].append(str(shared))
    boundary = write_boundary_with_runtime_ledger(one.agent, one.params)
    result = _execute(one.registry, 'write_file', {'path':str(shared/'yes.txt'), 'content':'shared'}, write_boundary=boundary)
    assert result.ok, result.output



def real_case(tmp_path):
    from agent_py_agent.agent.agent_core.runtime.loop_models import RunParams
    from agent_py_agent.agent.conversation.business_contracts import BusinessContextIntent
    from agent_py_agent.agent.conversation.session_tasks import SessionTaskDraft
    from agent_py_agent.agent.core import SimpleAgent
    from agent_py_agent.agent.settings import AgentConfig

    agent = SimpleAgent(AgentConfig(model_backend='echo', my_agent_home=str(tmp_path/'home')), tmp_path/'project')
    owner = agent.home_paths.owner_id
    sender = agent.conversation_store.threads.create(owner_id=owner, canonical_user_id='local-agent')
    target = agent.conversation_store.threads.create(owner_id=owner, canonical_user_id='local-agent')
    intent = BusinessContextIntent(owner, 'space-a', None, None)
    task = agent.conversation_store.session_tasks.create(SessionTaskDraft(sender.thread_id, target.thread_id, '测试订单'),
        now=1767311940.0, business_context_intent=intent,
        business_context_factory=lambda task_id: BusinessExecutionContext(owner, 'space-a', None, task_id, 1, None, (), (), 'business-context.v1'))
    context = BusinessExecutionContext.from_dict(task.metadata['business_context'])
    return agent, RunParams(source='background_main_agent', task_id=task.task_id, run_id=task.task_id, request_id='req-a',
        business_context=context, task_attributes={'conversation_thread_id':target.thread_id,
            'conversation_session_task_id':task.task_id, 'conversation_execution_workspace_explicit':False})


def test_real_session_task_keeps_original_directory_across_midnight(tmp_path, monkeypatch):
    from agent_py_agent.agent.agent_core import run_task_workspace_writer as module

    agent, params = real_case(tmp_path)
    monkeypatch.setattr(module, '_now_iso', lambda:'2026-01-01T23:59:00+00:00')
    before = module.managed_business_default_workspace(agent, params)
    monkeypatch.setattr(module, '_now_iso', lambda:'2026-01-05T00:01:00+00:00')
    after = module.managed_business_default_workspace(agent, params)
    assert after == before
    assert '2026-01-01' in str(after[0])


def test_default_business_workspace_creation_rejects_symlink_parent(tmp_path):
    from agent_py_agent.agent.agent_core.run_task_workspace_writer import (
        attach_run_task_workspace_context,
        managed_business_default_workspace,
    )
    from agent_py_agent.agent.common.nofollow_fs import NoFollowPathError

    agent, params = real_case(tmp_path)
    work, output = managed_business_default_workspace(agent, params)
    outside = tmp_path/'outside'
    outside.mkdir()
    work.parent.parent.mkdir(parents=True, exist_ok=True)
    work.parent.symlink_to(outside, target_is_directory=True)
    with pytest.raises(NoFollowPathError):
        attach_run_task_workspace_context(agent, params, '测试订单')
    assert list(outside.iterdir()) == []


@pytest.mark.parametrize('conflict', ['missing-task', 'wrong-target', 'wrong-owner'])
def test_real_session_task_workspace_requires_exact_original_binding(tmp_path, conflict):
    from dataclasses import replace

    from agent_py_agent.agent.agent_core.run_task_workspace_writer import (
        managed_business_default_workspace,
    )

    agent, params = real_case(tmp_path)
    if conflict == 'missing-task':
        context = replace(params.business_context, task_id='nonexistent')
        params = replace(params, business_context=context,
            task_attributes={**params.task_attributes, 'conversation_session_task_id':'nonexistent'})
    elif conflict == 'wrong-target':
        params = replace(params, task_attributes={**params.task_attributes, 'conversation_thread_id':'another-thread'})
    else:
        params = replace(params, business_context=replace(params.business_context, owner_id='other-owner'))
    with pytest.raises(PermissionError, match='BUSINESS_TASK_WORKSPACE_IDENTITY_MISMATCH'):
        managed_business_default_workspace(agent, params)


def test_default_business_roots_cannot_follow_another_order_symlink(tmp_path):
    from agent_py_agent.agent.common.nofollow_fs import NoFollowPathError

    one = case(tmp_path)
    other = case(tmp_path, 'order-b')
    one.work.rmdir()
    one.work.symlink_to(other.output, target_is_directory=True)
    with pytest.raises(NoFollowPathError):
        write_boundary_with_runtime_ledger(one.agent, one.params)
    assert list(other.output.iterdir()) == []
