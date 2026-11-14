"""授权commit到运行中原attempt的增量发现；原权限与稳定提示前缀不得被展示刷新替代。"""
from copy import deepcopy
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.agent_core._runtime_params import ToolLoopExecuteParams
from agent_py_agent.agent.agent_core.runtime.capability_refresh import refresh_subagent_capabilities
from agent_py_agent.agent.runtime_context import (
    restore_current_subagent_context,
    set_current_subagent_context,
)
from agent_py_agent.agent.subagents.capability_updates import observe_capability_updates
from agent_py_agent.agent.subagents.model_capabilities import CapabilityGrant, CapabilityRequest
from agent_py_agent.agent.subagents.model_task import RuntimeIdentity, SubAgentTask
from agent_py_agent.tests.test_tool_presentation_projection import (
    prepared as tool_surface,  # noqa: F401, F811
)


@pytest.fixture
def live(request, tmp_path):
    registry, *_ = request.getfixturevalue("tool_surface")
    task = SubAgentTask(id='child', goal='任意测试', thought='', plan=[], parent_id='parent', root_id='root',
        status='RUNNING', runner_active_attempt_id='attempt', state_revision=1,
        allowed_tools=['tool_search', 'presentation_optional_a'],
        base_allowed_tools=['tool_search', 'presentation_optional_a'],
        runtime_identity=RuntimeIdentity(service_owner_id='local/main', effective_principal_id='local/main'))
    class Manager:
        workspace = tmp_path
        loads = 0
        def load(self, run_id):
            assert run_id == task.id
            self.loads += 1
            return deepcopy(task)
    manager = Manager()
    manager.runner_context = SimpleNamespace(_build_write_boundary=lambda task: {
        'capability_write_roots': [root for g in task.capability_grants for root in g.path_scope]})
    agent = SimpleNamespace(tools=registry, subagents=manager, home_paths=SimpleNamespace(owner_id='local/main'), root=tmp_path)
    params = ToolLoopExecuteParams(user_prompt='保持原输入', memories=[], runtime_injections=[], prompt_files=[],
        tool_catalog_section='原稳定目录', tool_recommendations_section='', tool_context=[], effective_on_chunk=None,
        allowed_tools=list(task.allowed_tools), write_boundary={}, task_attributes={}, request_id='request',
        run_id=task.id, task_id='root', one_shot_tool_calls=set(), executed_tools=[], archive_tool_calls=[],
        attempt_id='attempt', tool_runtime_snapshot=registry.runtime_snapshot(allowed_tools=task.allowed_tools, run_id=task.id))
    previous = set_current_subagent_context(agent, run_id=task.id, attempt_id='attempt')
    try:
        yield agent, task, refresh_subagent_capabilities(agent, params)
    finally:
        restore_current_subagent_context(agent, previous)


def grant(task, name='presentation_optional_b'):
    task.state_revision += 1
    task.capability_revision += 1
    task.capability_requests.append(CapabilityRequest('req', task.id, '说明', '说明', status='GRANTED', requested_tools=[name]))
    task.capability_grants.append(CapabilityGrant('grant', 'req', task.id, tools=[name]))
    task.allowed_tools.append(name)


def test_committed_grant_reaches_same_attempt_without_replaying_or_reordering(live):
    agent, task, params = live
    params.executed_tools.append('already_executed')
    grant(task)
    observe_capability_updates(agent.subagents, task)
    updated = refresh_subagent_capabilities(agent, params)
    assert 'presentation_optional_b' in updated.tool_runtime_snapshot.available_tool_names
    assert updated.tool_catalog_section == params.tool_catalog_section
    assert updated.executed_tools == ['already_executed']
    assert params.tool_runtime_snapshot.runtime('presentation_optional_b') is None
    loads = agent.subagents.loads
    for _ in range(100):
        assert refresh_subagent_capabilities(agent, updated) is updated
    assert agent.subagents.loads == loads  # 未变epoch：零canonical重读与零快照重建。


def test_unapproved_allowed_name_cannot_expand_snapshot(live):
    agent, task, params = live
    task.allowed_tools.append('presentation_optional_b')
    task.state_revision += 1
    task.capability_revision += 1
    observe_capability_updates(agent.subagents, task)
    assert 'presentation_optional_b' not in refresh_subagent_capabilities(agent, params).tool_runtime_snapshot.available_tool_names


def test_revocation_narrows_new_discovery_and_old_snapshot_is_immutable(live):
    agent, task, params = live
    grant(task)
    observe_capability_updates(agent.subagents, task)
    updated = refresh_subagent_capabilities(agent, params)
    task.effective_permissions['disabled_tools'] = ['presentation_optional_b']
    task.state_revision += 1
    task.capability_revision += 1
    observe_capability_updates(agent.subagents, task)
    narrowed = refresh_subagent_capabilities(agent, updated)
    assert narrowed.tool_runtime_snapshot.runtime('presentation_optional_b') is None
    assert updated.tool_runtime_snapshot.runtime('presentation_optional_b') is not None


@pytest.mark.parametrize('field,value', [('parent_id','other-parent'), ('root_id','other-root'),
    ('runner_active_attempt_id','other-attempt'), ('status','CANCELLED')])
def test_changed_scope_or_closed_attempt_cannot_reuse_notification(live, field, value):
    agent, task, params = live
    setattr(task, field, value)
    task.state_revision += 1
    task.capability_revision += 1
    observe_capability_updates(agent.subagents, task)
    with pytest.raises(InterruptedError, match='CAPABILITY_REFRESH_SCOPE_CLOSED'):
        refresh_subagent_capabilities(agent, params)


def test_late_older_revision_cannot_restore_revoked_tool(live):
    agent, task, params = live
    grant(task)
    observe_capability_updates(agent.subagents, task)
    updated = refresh_subagent_capabilities(agent, params)
    old = deepcopy(task)
    task.effective_permissions['disabled_tools'] = ['presentation_optional_b']
    task.state_revision += 1
    task.capability_revision += 1
    observe_capability_updates(agent.subagents, task)
    observe_capability_updates(agent.subagents, old)
    assert refresh_subagent_capabilities(agent, updated).tool_runtime_snapshot.runtime('presentation_optional_b') is None


def test_expired_new_grant_is_removed_without_new_io(live, monkeypatch):
    from agent_py_agent.agent.agent_core.runtime import capability_refresh
    agent, task, params = live
    clock = [100.0]
    monkeypatch.setattr(capability_refresh.time, 'time', lambda: clock[0])
    grant(task)
    task.capability_grants[-1].expires_at = 101.0
    observe_capability_updates(agent.subagents, task)
    updated = refresh_subagent_capabilities(agent, params)
    assert updated.tool_runtime_snapshot.runtime('presentation_optional_b')
    reads = agent.subagents.loads
    clock[0] = 101.1
    expired = refresh_subagent_capabilities(agent, updated)
    assert expired.tool_runtime_snapshot.runtime('presentation_optional_b') is None
    assert agent.subagents.loads == reads


def test_foreign_owner_notification_invalidates_original_attempt(live):
    agent, task, params = live
    task.runtime_identity.service_owner_id = 'local/other'
    task.state_revision += 1
    task.capability_revision += 1
    observe_capability_updates(agent.subagents, task)
    with pytest.raises(InterruptedError, match='CAPABILITY_REFRESH_SCOPE_CLOSED'):
        refresh_subagent_capabilities(agent, params)


def test_wrong_thread_attempt_cannot_borrow_refresh_source(live):
    agent, task, params = live
    previous = set_current_subagent_context(agent, run_id='sibling', attempt_id='other')
    try:
        grant(task)
        observe_capability_updates(agent.subagents, task)
        with pytest.raises(PermissionError, match='CAPABILITY_REFRESH_IDENTITY_MISMATCH'):
            refresh_subagent_capabilities(agent, params)
    finally:
        restore_current_subagent_context(agent, previous)


def test_actual_canonical_grant_commit_publishes_only_after_mutation(tmp_path):
    from agent_py_agent.agent.core import SimpleAgent
    from agent_py_agent.agent.settings.config import AgentConfig
    from agent_py_agent.agent.subagents.capability_updates import subscribe_capability_updates
    from agent_py_agent.agent.subagents.services.lifecycle import (
        RecordCapabilityGrantParams,
        RecordCapabilityRequestParams,
    )
    agent = SimpleAgent(AgentConfig(model_backend='echo', my_agent_home=str(tmp_path/'home'), enable_plugins=False), tmp_path/'project')
    task = agent.subagents.create_run(goal='通用授权链', thought='本地合同回归', plan=[], allowed_tools=['tool_search'])
    source = subscribe_capability_updates(agent.subagents, task.id)
    request = agent.subagents.lifecycle.record_capability_request(task.id, RecordCapabilityRequestParams(
        problem='需要只读目录', needed_capability='列目录', requested_tools=['list_files']))
    before, _ = source.current()
    agent.subagents.lifecycle.record_capability_grant(task.id, RecordCapabilityGrantParams(
        request_id=request.id, tools=['list_files']))
    after, committed = source.current()
    assert after > before
    assert committed.capability_requests[0].status == 'GRANTED'
    assert committed.capability_grants[0].grant_to_run_id == task.id
    assert 'list_files' in committed.allowed_tools


def test_prior_attempt_grant_expires_but_independent_same_tool_and_base_path_remain(live, monkeypatch):
    from agent_py_agent.agent.agent_core.runtime import capability_refresh
    agent, task, params = live
    clock = [100.0]
    monkeypatch.setattr(capability_refresh.time, 'time', lambda: clock[0])
    grant(task)
    task.base_allowed_tools.append('presentation_optional_b')
    task.capability_grants[-1].expires_at = 101.0
    task.capability_grants[-1].path_scope = ['/synthetic/temporary']
    observe_capability_updates(agent.subagents, task)
    updated = refresh_subagent_capabilities(agent, params)
    assert updated.write_boundary['capability_write_roots'] == ['/synthetic/temporary']
    clock[0] = 101.1
    expired = refresh_subagent_capabilities(agent, updated)
    assert expired.tool_runtime_snapshot.runtime('presentation_optional_b') is not None
    assert expired.write_boundary['capability_write_roots'] == []


def test_unrelated_heartbeat_does_not_copy_task_or_walk_grants(live, monkeypatch):
    from agent_py_agent.agent.subagents import capability_updates
    agent, task, params = live
    def fail(_):
        pytest.fail('无关心跳不应复制授权材料')
    monkeypatch.setattr(capability_updates, '_authority_view', fail)
    for _ in range(100):
        task.state_revision += 1
        observe_capability_updates(agent.subagents, task)
    assert refresh_subagent_capabilities(agent, params) is params


def test_resubscribe_same_attempt_and_old_source_both_receive_revocation(live):
    from agent_py_agent.agent.subagents.capability_updates import subscribe_capability_updates
    agent, task, params = live
    original = params._capability_update_source
    assert subscribe_capability_updates(agent.subagents, task.id) is original
    task.runner_active_attempt_id = 'next-attempt'
    task.state_revision += 1
    newer = subscribe_capability_updates(agent.subagents, task.id)
    assert newer is not original
    observe_capability_updates(agent.subagents, task)
    assert original.current()[1] is None
    assert newer.current()[1].runner_active_attempt_id == 'next-attempt'


def test_released_source_is_not_retained_by_future_canonical_updates(live):
    from agent_py_agent.agent.subagents.capability_updates import release_capability_updates
    agent, task, params = live
    source = params._capability_update_source
    before = source.current()[0]
    release_capability_updates(source)
    grant(task)
    observe_capability_updates(agent.subagents, task)
    assert source.current()[0] == before


def test_legacy_ambiguous_expired_grant_has_explicit_reauthorization_instruction(live, monkeypatch):
    from agent_py_agent.agent.agent_core.runtime import capability_refresh
    agent, task, params = live
    monkeypatch.setattr(capability_refresh.time, 'time', lambda: 100.0)
    task.base_allowed_tools = None
    grant(task)
    task.capability_grants[-1].expires_at = 99.0
    observe_capability_updates(agent.subagents, task)
    updated = refresh_subagent_capabilities(agent, params)
    assert updated.tool_runtime_snapshot.runtime('presentation_optional_b') is None
    assert any('LEGACY_CAPABILITY_BASE_UNKNOWN' in line and 'capability_request' in line for line in updated.tool_context)


def test_resumed_loop_after_release_rebinds_even_when_local_epoch_number_repeats(live):
    from agent_py_agent.agent.subagents.capability_updates import release_capability_updates
    agent, task, params = live
    release_capability_updates(params._capability_update_source)
    grant(task)
    updated = refresh_subagent_capabilities(agent, params)
    assert updated._capability_update_source is not params._capability_update_source
    assert updated.tool_runtime_snapshot.runtime('presentation_optional_b') is not None


def test_both_parent_creation_paths_drop_expired_grant(live, monkeypatch):
    from agent_py_agent.agent.agent_core.orchestration.create_policy import (
        _parent_tool_authority_names,
    )
    from agent_py_agent.agent.subagents import capability_authority
    from agent_py_agent.agent.subagents.services.hierarchy.scheduler import _current_parent_tools
    agent, task, _ = live
    monkeypatch.setattr(capability_authority.time, 'time', lambda: 100.0)
    grant(task)
    task.capability_grants[-1].expires_at = 99.0
    assert 'presentation_optional_b' not in _current_parent_tools(task)
    assert 'presentation_optional_b' not in _parent_tool_authority_names(agent)


def test_many_expired_path_grants_do_not_add_hot_loop_reads(live, monkeypatch):
    from agent_py_agent.agent.agent_core.runtime import capability_refresh
    agent, task, params = live
    monkeypatch.setattr(capability_refresh.time, 'time', lambda: 100.0)
    task.capability_grants = [CapabilityGrant(str(i), str(i), task.id,
        tools=['presentation_optional_b'], path_scope=['/synthetic/old'], expires_at=99.0) for i in range(2000)]
    task.state_revision += 1
    task.capability_revision += 1
    observe_capability_updates(agent.subagents, task)
    updated = refresh_subagent_capabilities(agent, params)
    assert not updated._capability_recheck_tools
    loads = agent.subagents.loads
    for _ in range(100):
        assert refresh_subagent_capabilities(agent, updated, tool_name='tool_search') is updated
    assert agent.subagents.loads == loads


def test_actual_temporary_tool_use_rechecks_canonical_not_just_notification(live):
    agent, task, params = live
    grant(task)
    observe_capability_updates(agent.subagents, task)
    updated = refresh_subagent_capabilities(agent, params)
    # 模拟其它进程已写canonical但心跳尚未来；真实临时工具调用不得等待5秒通知。
    task.effective_permissions['disabled_tools'] = ['presentation_optional_b']
    task.state_revision += 1
    task.capability_revision += 1
    loads = agent.subagents.loads
    rejected = refresh_subagent_capabilities(agent, updated, tool_name='presentation_optional_b')
    assert agent.subagents.loads == loads + 1
    assert rejected.tool_runtime_snapshot.runtime('presentation_optional_b') is None


def test_compact_releases_only_registered_refresh_aliases_and_keeps_frozen_snapshots(live):
    from dataclasses import replace

    from agent_py_agent.agent.agent_core.runtime.capability_refresh import (
        release_compact_provider_history,
    )

    agent, task, params = live
    history = [{'role': 'user', 'content': '大历史' * 10000}]
    object.__setattr__(params, 'provider_history_messages', history)
    frozen_snapshot = params.tool_runtime_snapshot
    grant(task)
    observe_capability_updates(agent.subagents, task)
    updated = refresh_subagent_capabilities(agent, params)
    independent_view = replace(updated)  # 没有经refresh登记的独立视图不属于此次解绑。
    other_request = refresh_subagent_capabilities(agent, replace(updated, request_id='other-request'))
    release_compact_provider_history(updated)
    assert params.provider_history_messages == updated.provider_history_messages == []
    assert independent_view.provider_history_messages is history
    assert other_request.provider_history_messages is history
    assert frozen_snapshot.runtime('presentation_optional_b') is None
    assert updated.tool_runtime_snapshot.runtime('presentation_optional_b') is not None


@pytest.mark.parametrize('first_subscription', [False, True])
def test_history_release_serializes_with_inflight_refresh_without_resurrection(live, monkeypatch, first_subscription):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    from agent_py_agent.agent.agent_core.runtime.capability_refresh import (
        release_compact_provider_history,
    )

    agent, task, params = live
    if first_subscription:
        from dataclasses import replace

        params = replace(params, _capability_update_source=None, _provider_history_owners=None)
    object.__setattr__(params, 'provider_history_messages', [{'role': 'user', 'content': '长历史' * 10000}])
    grant(task)
    observe_capability_updates(agent.subagents, task)
    entered, proceed, releasing, released = Event(), Event(), Event(), Event()
    original = agent.tools.runtime_snapshot

    def snapshot(*args, **kwargs):
        entered.set()
        assert proceed.wait(5), '测试必须释放快照构建屏障'
        return original(*args, **kwargs)

    monkeypatch.setattr(agent.tools, 'runtime_snapshot', snapshot)

    def release():
        releasing.set()
        release_compact_provider_history(params)
        released.set()

    def refresh_in_runner():
        previous = set_current_subagent_context(agent, run_id=params.run_id, attempt_id=params.attempt_id)
        try:
            return refresh_subagent_capabilities(agent, params)
        finally:
            restore_current_subagent_context(agent, previous)

    with ThreadPoolExecutor(max_workers=2) as pool:
        refresh = pool.submit(refresh_in_runner)
        try:
            assert entered.wait(5)
            release_job = pool.submit(release)
            assert releasing.wait(5)
            assert not released.wait(.05), '解绑不能越过正在构建的本请求能力副本'
        finally:
            proceed.set()
        updated = refresh.result(timeout=5)
        release_job.result(timeout=5)
    assert params.provider_history_messages == updated.provider_history_messages == []


def test_history_owner_group_is_weak_bounded_and_foreign_owner_cannot_change_it(live):
    import gc

    agent, task, params = live
    owners = params._provider_history_owners
    old_owner = agent.home_paths.owner_id
    agent.home_paths.owner_id = 'foreign-owner'
    try:
        with pytest.raises(PermissionError, match='OWNER_MISMATCH'):
            refresh_subagent_capabilities(agent, params)
        assert params._provider_history_owners is owners
    finally:
        agent.home_paths.owner_id = old_owner
    for _ in range(100):
        task.state_revision += 1
        task.capability_revision += 1
        observe_capability_updates(agent.subagents, task)
        params = refresh_subagent_capabilities(agent, params)
    gc.collect()
    assert len(owners.views) <= 4, '弱所有者只能随活跃别名，不随刷新次数累积'


def test_concurrent_same_owner_different_scope_history_owners_do_not_cross(live):
    from concurrent.futures import ThreadPoolExecutor
    from dataclasses import replace
    from threading import Barrier

    from agent_py_agent.agent.agent_core.runtime.capability_refresh import (
        release_compact_provider_history,
    )

    agent, _, params = live
    history = [{'role': 'user', 'content': '并发私有输入' * 1000}]
    object.__setattr__(params, 'provider_history_messages', history)
    other = refresh_subagent_capabilities(agent, replace(params, request_id='different-request',
        task_id='other-task', context_scope='task_local'))
    assert other._provider_history_owners is not params._provider_history_owners
    barrier = Barrier(2)

    def release():
        barrier.wait(timeout=5)
        release_compact_provider_history(params)

    def keep():
        barrier.wait(timeout=5)
        for _ in range(100):
            assert other.provider_history_messages is history

    with ThreadPoolExecutor(max_workers=2) as pool:
        jobs = [pool.submit(release), pool.submit(keep)]
        for job in jobs:
            job.result(timeout=5)
    assert params.provider_history_messages == []
    assert other.provider_history_messages is history


def test_disabled_tools_remain_empty_across_initial_subscription_and_grant(live):
    from dataclasses import replace

    agent, task, original = live
    agent.config = SimpleNamespace(enable_tools=False)
    params = replace(original, _capability_update_source=None, _provider_history_owners=None)
    initial = refresh_subagent_capabilities(agent, params)
    assert initial.allowed_tools == []
    assert not initial.tool_runtime_snapshot.available_tool_names
    grant(task)
    observe_capability_updates(agent.subagents, task)
    updated = refresh_subagent_capabilities(agent, initial)
    assert updated.allowed_tools == []
    assert not updated.tool_runtime_snapshot.available_tool_names
    assert original.tool_runtime_snapshot.available_tool_names
    assert agent.tools.runtime_snapshot(allowed_tools=task.allowed_tools).available_tool_names
