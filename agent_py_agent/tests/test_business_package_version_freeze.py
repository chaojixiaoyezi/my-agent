"""原Owner安装事实进入唯一任务快照；换代/撤销只收窄所选包，历史记录不冒称冻结。"""
from dataclasses import replace
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.capability.package_build import build_capability_pack
from agent_py_agent.agent.capability.package_provider import (
    enabled_capability_packages,
    read_capability_member,
)
from agent_py_agent.agent.conversation.business_access import BusinessActor, BusinessError
from agent_py_agent.agent.conversation.business_context import (
    resolve_business_context,
    revalidate_business_context,
)
from agent_py_agent.agent.conversation.business_contracts import BusinessExecutionContext
from agent_py_agent.agent.conversation.business_spaces import BusinessSpaceService
from agent_py_agent.agent.gateway_parts.business_host_ports import (
    build_default_business_package_reference_resolver,
    build_default_task_business_context,
)
from agent_py_agent.agent.plugin_activation import PluginActivationRequest
from agent_py_agent.agent.plugin_content_activation import PluginContentActivation
from agent_py_agent.agent.plugin_install_store import PluginInstallStore
from agent_py_agent.agent.plugin_installation import PluginInstallationError, PluginInstallRequest
from agent_py_agent.agent.plugin_package import inspect_plugin_package
from agent_py_agent.agent.plugin_update import PluginUpdateRequest
from agent_py_agent.agent.user_space.owner_resolver import OwnerIdentity, ensure_owner_home


def package(name, version='1.0.0'):
    return inspect_plugin_package(build_capability_pack({'plugin_id': name, 'version': version,
        'summary': '版本冻结夹具', 'capability': {'description': '只读测试', 'keywords': ['freeze'],
        'entry_document': 'README.md'}, 'files': [{'path': 'README.md'}], 'settings_schema': {}},
        {'README.md': ('# ' + name + ' ' + version + '\n').encode()}).payload)


def activate(store, entry, key):
    activation = PluginContentActivation(key, entry.manifest.plugin_id, entry.package_sha256,
        entry.revision, entry.settings_revision)
    return store.change_activation(PluginActivationRequest(key, entry.revision, activation)).installation


@pytest.fixture
def world(tmp_path):
    owner = ensure_owner_home(tmp_path / 'home')
    host = SimpleNamespace(home_paths=SimpleNamespace(owner_id=owner.owner_id, owner_home_dir=owner.home_dir))
    service = BusinessSpaceService(owner.home_dir, owner_id=owner.owner_id, enabled=True,
        package_reference_resolver=build_default_business_package_reference_resolver(host))
    actor = BusinessActor(owner.owner_id, 'operator', service, lambda *_: True)
    store = PluginInstallStore(owner)
    entries = {}
    for name in ('pack-a', 'pack-b'):
        entry = store.install(PluginInstallRequest(package(name), 'install-' + name, 0)).installation
        entries[name] = activate(store, entry, 'activate-' + name)
    spaces = {}
    for name in entries:
        sid = service.create({'name': name}, actor=actor, idempotency_key='create-' + name)['space_id']
        service.packages('spaces.packages.attach', {'space_id': sid, 'package_refs': ['capability:' + name]},
            actor=actor, expected_revision=0, idempotency_key='attach-' + name)
        spaces[name] = sid
    return SimpleNamespace(owner=owner, host=host, service=service, actor=actor, store=store,
        entries=entries, spaces=spaces)


def freeze(world, name='pack-a'):
    return resolve_business_context(actor=world.actor, task_id='task-' + name,
        requested_space_id=world.spaces[name], project_ref=None, binding_id=None)


def release(world, name):
    entry = world.entries[name]
    revoked = world.store.change_activation(PluginActivationRequest('revoke-' + name, entry.revision,
        replace(entry.activation, phase='revoked'))).installation
    result, _ = world.store.release_activation(world.owner, None, revoked, 'release-' + name)
    world.entries[name] = result.installation
    return result.installation


def test_original_digest_activation_and_codecs_are_immutable(world):
    frozen = freeze(world)
    binding, = frozen.package_versions
    assert binding.version_ref == world.entries['pack-a'].package_sha256
    assert binding.activation_id == world.entries['pack-a'].activation_id
    assert frozen.package_versions_state == 'frozen'
    encoded = frozen.to_dict()
    assert BusinessExecutionContext.from_dict(encoded) == frozen
    encoded['package_versions'][0]['version_ref'] = 'mutated-projection'
    assert binding.version_ref == world.entries['pack-a'].package_sha256
    host = SimpleNamespace(conversation_store=SimpleNamespace(session_tasks=SimpleNamespace(
        load=lambda _: SimpleNamespace(metadata={'business_context': frozen.to_dict()}))))
    assert build_default_task_business_context(host)(frozen.task_id) == frozen
    assert build_default_task_business_context(host)('other-task') is None
    revalidate_business_context(actor=world.actor, context=frozen)


def test_upgrade_rejects_original_activation_without_affecting_other_package(world):
    a, b = freeze(world), freeze(world, 'pack-b')
    snapshot = enabled_capability_packages(world.owner)[0]
    old = release(world, 'pack-a')
    updated = world.store.update_package(PluginUpdateRequest('pack-a', package('pack-a', '2.0.0'),
        'update-pack-a', old.revision)).installation
    activate(world.store, updated, 'activate-pack-a-v2')
    with pytest.raises(BusinessError, match='PACKAGE_VERSION_MISMATCH') as refusal:
        revalidate_business_context(actor=world.actor, context=a, package_ref='pack-a')
    assert refusal.value.details['effect_outcome'] == 'not_started'
    assert refusal.value.details['expected_version_ref'] == a.package_versions[0].version_ref
    revalidate_business_context(actor=world.actor, context=b, package_ref='pack-b')
    with pytest.raises(PluginInstallationError):
        snapshot.reader('README.md')
    assert snapshot.package_sha256 == a.package_versions[0].version_ref
    assert a.space_revision == 1


@pytest.mark.parametrize('remove', [False, True])
def test_disable_and_remove_are_not_replaced_by_latest_or_other_owner(world, remove):
    a, b = freeze(world), freeze(world, 'pack-b')
    inactive = release(world, 'pack-a')
    if remove:
        world.store.remove('remove-pack-a', 'pack-a', inactive)
    with pytest.raises(BusinessError, match='PACKAGE_VERSION_UNAVAILABLE'):
        revalidate_business_context(actor=world.actor, context=a, package_ref='pack-a')
    revalidate_business_context(actor=world.actor, context=b, package_ref='pack-b')
    other = ensure_owner_home(world.owner.root, OwnerIdentity.local_user('another'))
    other_service = BusinessSpaceService(other.home_dir, owner_id=other.owner_id, enabled=True)
    other_actor = BusinessActor(other.owner_id, 'other', other_service, lambda *_: True)
    with pytest.raises(BusinessError, match='SPACE_SCOPE_MISMATCH'):
        revalidate_business_context(actor=other_actor, context=a)


def test_historical_refs_stay_unproven_but_unrelated_reads_keep_original_gate(world):
    original = freeze(world)
    encoded = original.to_dict()
    encoded.pop('package_versions')
    encoded.pop('package_versions_state')
    legacy = BusinessExecutionContext.from_dict(encoded)
    assert legacy.package_versions_state == 'legacy_unproven' and legacy.package_versions == ()
    release(world, 'pack-a')
    # 旧选择可能是method，也可能是package，不能猜成冻结包而封死查旧产物/取消。
    revalidate_business_context(actor=world.actor, context=legacy)
    explicit_method = replace(legacy, selected_method_refs=('method:historical-non-package',))
    revalidate_business_context(actor=world.actor, context=explicit_method)
    denied = BusinessActor(world.owner.owner_id, 'denied', world.service, lambda *_: False)
    with pytest.raises(BusinessError, match='BUSINESS_ACCESS_DENIED'):
        revalidate_business_context(actor=denied, context=legacy)


def test_action_policy_publishes_only_bounded_public_version_facts(world, monkeypatch):
    from agent_py_agent.agent.conversation import business_context
    from agent_py_agent.agent.tooling.action_policy import _business_scope_decision

    context = freeze(world)
    request = SimpleNamespace(business_context=context, business_actor=world.actor,
        call=SimpleNamespace(arguments={}))
    runtime = SimpleNamespace(model_spec=SimpleNamespace(name='read_file'), handler=object())

    def deny(**_):
        raise BusinessError('PACKAGE_VERSION_MISMATCH', details={
            'expected_version_ref': 'old-content', 'actual_version_ref': 'new-content',
            'next_action': '请创建采用新版本的新任务，不要原样重试。',
            'raw_exception': '/other-owner/private-token', 'foreign_owner': 'private',
            'actual_activation_id': 'x' * 513})

    monkeypatch.setattr(business_context, 'revalidate_business_context', deny)
    decision = _business_scope_decision(request, runtime)
    assert decision.status == 'deny' and decision.reason_codes == ('TOOL_EXECUTION_DENIED',)
    assert decision.evidence['expected_version_ref'] == 'old-content'
    assert decision.evidence['actual_version_ref'] == 'new-content'
    assert 'next_action' in decision.evidence
    assert not {'raw_exception', 'foreign_owner', 'actual_activation_id'} & decision.evidence.keys()


def test_registry_executor_checks_frozen_package_and_keeps_unrelated_reads(world, monkeypatch):
    from agent_py_agent.agent.tooling.models import BaseTool, ToolHandlerOutcome
    from agent_py_agent.agent.tooling.registry import ToolRegistry
    from agent_py_agent.tests._tool_runtime_harness import (
        canonical_test_call,
        make_test_model_spec,
        make_test_runtime_policy,
        runtime_snapshot_for_tools,
    )

    called = []

    class Reader(BaseTool):
        def __init__(self, name):
            self.model_spec = make_test_model_spec(name, input_schema={'type': 'object',
                'properties': {'package_id': {'type': 'string'}, 'source_ref': {'type': 'object'},
                    'action': {'type': 'string'}, 'expected_package_sha256': {'type': 'string'},
                    'expected_activation_id': {'type': 'string'}},
                'additionalProperties': False})
            self.runtime_policy = make_test_runtime_policy()

        def execute(self, params):
            called.append(self.model_spec.name)
            return ToolHandlerOutcome(self.model_spec.name, True, 'read')

    a = freeze(world)
    old = release(world, 'pack-a')
    updated = world.store.update_package(PluginUpdateRequest('pack-a', package('pack-a', '2.0.0'),
        'update-registry-a', old.revision)).installation
    activate(world.store, updated, 'activate-registry-a-v2')
    registry = SimpleNamespace(workspace_root=world.owner.home_dir, workspace_roots=(),
        path_access_mode='normal', path_dangerous_roots=(), owner_scope_root=str(world.owner.home_dir),
        runtime_guard_policy=None, _construction_params=SimpleNamespace(permission_state_reader=None),
        approval_mode_reader=lambda: 'auto', operation_store=None, operation_store_required=False,
        operation_owner_id=world.owner.owner_id, owner_type='main_agent', plugin_gate_reviewer=None)
    tools = {name: Reader(name) for name in ('skill_search', 'read_file', 'write_file')}
    snapshot = runtime_snapshot_for_tools(tools)

    def execute(name, params, **scope):
        return ToolRegistry.execute_tool(registry, canonical_test_call(snapshot, name, params),
            runtime_snapshot=snapshot, write_boundary=None, **scope)

    rejected = execute('skill_search', {'package_id': 'pack-a'}, business_context=a, business_actor=world.actor)
    assert not rejected.result.ok and rejected.result.handler_executed is False
    assert rejected.decision.reason_codes == ('TOOL_EXECUTION_DENIED',)
    assert rejected.result.reported_error_code == 'PACKAGE_VERSION_MISMATCH'
    assert '下一步' in rejected.result.output
    assert rejected.decision.evidence['expected_version_ref'] == a.package_versions[0].version_ref
    assert called == []
    padded = execute('skill_search', {'package_id': '  pack-a  '}, business_context=a, business_actor=world.actor)
    assert padded.result.reported_error_code == 'PACKAGE_VERSION_MISMATCH' and called == []
    copied = execute('write_file', {'source_ref': {'kind': 'capability_package', 'package_id': 'pack-a'}},
        business_context=a, business_actor=world.actor)
    assert copied.result.reported_error_code == 'PACKAGE_VERSION_MISMATCH' and called == []
    allowed = execute('read_file', {}, business_context=a, business_actor=world.actor)
    assert allowed.result.ok and called == ['read_file']
    # 未带业务上下文的常规用户调用沿原授权，不借ToolCall.args伪造冻结数据。
    ordinary = execute('skill_search', {'package_id': 'pack-a'})
    assert ordinary.result.ok and called == ['read_file', 'skill_search']
    from agent_py_agent.agent.agent_core.self_map_bootstrap import _WorkflowToolDispatcher
    from agent_py_agent.agent.conversation import business_context

    monkeypatch.setattr(business_context, 'business_execution_policy_scope',
        lambda _: {'business_context': a, 'business_actor': world.actor})
    registry.execute_tool = lambda *args, **kwargs: ToolRegistry.execute_tool(registry, *args, **kwargs)
    dispatcher = object.__new__(_WorkflowToolDispatcher)
    dispatcher.agent = SimpleNamespace(home_paths=SimpleNamespace(owner_id=world.owner.owner_id), tools=registry)
    dispatcher.params = SimpleNamespace(run_id='test-run', attempt_id='test-attempt', task_id=a.task_id,
        request_id='test-turn', task_attributes={})
    dispatcher.registry, dispatcher.snapshot, dispatcher.boundary = registry, snapshot, None
    dispatcher.invoke = lambda operation, **_: operation()
    called.clear()
    from agent_py_agent.agent.workflows.models import StepExecutionRecord, WorkflowRunRecord

    # 内部dispatcher消费实际领域记录；包授权探针不能靠缺字段的空对象绕过步骤合同。
    step = StepExecutionRecord('package-read', '包版本读取', 'tool_call', 'running', 0, 0, 0, False,
        escalation_result={'attempt_id': 'package-effect-attempt'})
    run = WorkflowRunRecord('package-workflow', 'package-probe', world.owner.owner_id,
        'manual', 'running', 0, 0, 0, steps=(step,))
    result = dispatcher.execute_scoped('skill_search', {'package_id': 'pack-a'},
        owner_id=world.owner.owner_id, space_id=a.space_id, idempotency_key='workflow-step',
        logical_step_key='workflow-step:package-workflow:package-read', run=run, step=step)
    assert result['ok'] is False and result['effect_outcome'] == 'not_started'
    assert 'PACKAGE_VERSION_MISMATCH' in str(result) and called == []
    from agent_py_agent.agent.agent_core import tool_runtime_ledger
    from agent_py_agent.agent.capability import package_selection_authority

    monkeypatch.setattr(tool_runtime_ledger, 'write_boundary_with_runtime_ledger', lambda *_: None)
    dispatcher.params.conversation_turn_id = 'test-turn'
    policy = package_selection_authority.package_entry_policy(dispatcher.agent, dispatcher.params,
        snapshot, {'action': 'get', 'package_id': 'pack-a',
            'expected_package_sha256': updated.package_sha256}, claim_id='automatic-entry')
    assert policy.status == 'deny' and policy.evidence['reported_error_code'] == 'PACKAGE_VERSION_MISMATCH'





def test_ordinary_scope_does_not_load_business_services(monkeypatch):
    from agent_py_agent.agent.conversation.business_context import business_execution_policy_scope
    from agent_py_agent.agent.gateway_parts import business_api

    monkeypatch.setattr(business_api, 'local_business_context',
        lambda *_args, **_kwargs: pytest.fail('无业务上下文不得加载服务'))
    assert business_execution_policy_scope(SimpleNamespace(_current_run_params=None)) == {}
    assert business_execution_policy_scope(SimpleNamespace(
        _current_run_params=SimpleNamespace(business_context=None))) == {}


def test_bot_registry_transports_host_scope_without_relaxing_readonly_wall(world, monkeypatch):
    from agent_py_agent.agent.conversation.bot_tool_policy import (
        _BotToolRegistry as BotToolRegistry,
    )
    from agent_py_agent.agent.tooling.registry import ToolRegistry

    monkeypatch.setattr(ToolRegistry, 'execute_tool', lambda _self, call, **kwargs: kwargs)
    bot = object.__new__(BotToolRegistry)
    bot._construction_params = SimpleNamespace(workspace_root=world.owner.home_dir)
    bot.workspace_root = world.owner.home_dir
    bot._bot_guard = SimpleNamespace(policy={'bot_mode': 'query_readonly'}, check=lambda _: None)
    ordinary = bot.execute_tool(object())
    assert ordinary['business_context'] is None and ordinary['business_actor'] is None
    assert ordinary['write_boundary']['allowed_write_roots'] == []
    context = freeze(world)
    transported = bot.execute_tool(object(), business_context=context, business_actor=world.actor)
    assert transported['business_context'] is context and transported['business_actor'] is world.actor
    assert transported['write_boundary']['allowed_write_roots'] == []
    assert transported['write_boundary']['read_scope_mode'] == 'exact'
    assert transported['pre_handler_gate'](object()) is None
