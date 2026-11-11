"""W12 公共接口合同；只使用临时目录，不启动真实通道或 Gateway。"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict, fields, replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.conversation.channels import DeliveryContext, DeliveryReceipt
from agent_py_agent.agent.runtime_errors import environment_cause_fields
from agent_py_agent.agent.tooling.models import (
    ToolHandlerOutcome,
    ToolOperationReconciliationContext,
)
from agent_py_agent.agent.workflows import models, runtime_sdk


def test_tool_outcome_retains_old_positional_contract_and_new_failure_facts():
    old = ToolHandlerOutcome('demo', True, 'business output', 'call-1')
    assert old.output == 'business output' and old.call_id == 'call-1'
    assert {'operation_id', 'attempt_id', 'workflow_run_id', 'phase', 'component_receipts', 'diagnostic_ref'} <= {
        item.name for item in fields(old)}
    new = replace(old, ok=False, operation_id='op', attempt_id='attempt', workflow_run_id='wf',
                  phase='persistence', effect_outcome='unknown', component_receipts=({'component_id': 'text'},),
                  diagnostic_ref='diag:1')
    assert ToolHandlerOutcome(**asdict(new)) == new
    assert (new.operation_id, new.workflow_run_id, new.phase) == ('op', 'wf', 'persistence')


def test_reconciliation_context_legacy_and_first_dispatch_facts():
    old = ToolOperationReconciliationContext('owner', 'run', 'task', 'op', 'demo', 'hash', 'key', 'scope', {})
    assert {'first_dispatch_at', 'component_receipts'} <= {item.name for item in fields(old)}
    assert old.first_dispatch_at is None
    new = replace(old, first_dispatch_at=123.0, component_receipts=({'message_key': 'original'},))
    assert ToolOperationReconciliationContext(**asdict(new)) == new


def test_channel_old_construction_and_frozen_provider_topic_fields():
    old = DeliveryContext('feishu', 'target', 'reply', 'conversation', 'anchor', '', 'request', 'internal-thread')
    assert {'reply_policy', 'provider_thread_id', 'root_id', 'parent_id'} <= {item.name for item in fields(old)}
    new = replace(old, reply_policy='thread', provider_thread_id='provider-topic', root_id='root', parent_id='parent')
    assert new.thread_id == 'internal-thread' and new.reply_to == 'anchor'
    assert DeliveryContext(**asdict(new)) == new
    receipt = DeliveryReceipt('feishu', 'target', 'text')
    assert 'component_receipts' in {item.name for item in fields(receipt)}
    from agent_py_agent.agent.adapter.protocol import ChannelStopResult, ComponentDeliveryReceipt

    component = ComponentDeliveryReceipt('c1', 'key', first_dispatch_at=123.0, phase='execution',
                                         effect_outcome='unknown', upload_ref='upload:1', provider_code=100)
    receipt = replace(receipt, component_receipts=(component,))
    assert receipt.component_receipts[0].provider_message_id is None
    assert ChannelStopResult().status == 'exit_unconfirmed'
    with pytest.raises(ValueError):
        ChannelStopResult('success')


def test_workflow_results_keep_positional_and_json_roundtrip():
    script = models.ScriptExecutionResult('s', False, 1, 1.0, 10.0, 11.0, {}, '', '')
    run = models.WorkflowRunRecord('wf-run', 'wf', 'owner', 'manual', 'failed', 10.0, 11.0, 1.0)
    for dto in (script, run):
        assert {'execution_definition_ref', 'phase', 'effect_outcome', 'breakpoint', 'confirmed_step_refs'} <= {
            item.name for item in fields(dto)}
    run = replace(run, execution_definition_ref='sha256:definition', phase='persistence', effect_outcome='unknown',
                  breakpoint={'step_id': 's2'}, confirmed_step_refs=('s1',))
    assert models.WorkflowRunRecord.from_dict(json.loads(json.dumps(run.to_dict()))) == run
    assert models.WorkflowRunRecord.from_dict({key: value for key, value in run.to_dict().items()
        if key not in {'execution_definition_ref', 'phase', 'effect_outcome', 'confirmed_step_refs'}}).phase == ''
    from agent_py_agent.agent.plugin_host.execution_policy import AssetGrant

    old = AssetGrant('script', 's', 1, 'a' * 64)
    assert 'execution_definition_ref' in {item.name for item in fields(old)}
    current = replace(old, execution_definition_ref='sha256:definition')
    assert replace(current, revision=2).execution_definition_ref == current.execution_definition_ref
    assert AssetGrant(**asdict(current)) == current


def test_gateway_public_extension_preserves_all_legacy_fields():
    from agent_py_agent.cli import models as cli_models

    assert hasattr(cli_models, 'GatewayReadiness')
    dto = cli_models.GatewayReadiness('ready', 'GATEWAY_READY', 123, True, True, 'running', 'state', 10.0, 11.0)
    assert dto.load_errors == {}
    new = replace(dto, restart_request_id='restart', expected_generation={'started_at': 10.0},
                  observed_generation={'started_at': 10.0}, ready_observation={'source': 'state'},
                  cleanup_result={'complete': False}, forced_after_drain_timeout=True,
                  unresolved_operation_refs=('op',), notification_results=({'status': 'skipped', 'reason_code': 'missing_ref'},))
    assert cli_models.GatewayReadiness(**asdict(new)) == new


def test_recovery_eligibility_adapts_only_explicit_structured_legacy_reason():
    from agent_py_agent.agent.agent_core.runtime import loop_models

    assert hasattr(loop_models, 'RecoveryEligibility')
    eligibility = loop_models.RecoveryEligibility(False, 'RECOVERY_UNKNOWN', 7, 123.0, None)
    assert loop_models.RecoveryEligibility(**asdict(eligibility)) == eligibility
    adapt = loop_models.RecoveryEligibility.from_block
    assert adapt(None).eligible is True
    blocked = adapt({'schema_version': 'main-agent-recovery-block.v1', 'reason': 'authority_state_unreadable',
                     'source_revision': 7, 'notice': '交接完成'})
    assert blocked.eligible is False and blocked.reason_code == 'RECOVERY_AUTHORITY_UNREADABLE'
    assert blocked.source_revision == 7 and blocked.handoff_ref is None
    assert adapt({'notice': '已经恢复，可以执行'}).reason_code == 'RECOVERY_UNKNOWN'


@pytest.mark.parametrize('sql,code,name', [
    ('select * from absent_table', sqlite3.SQLITE_ERROR, 'SQLITE_ERROR'),
    ('insert into unique_values values (1)', sqlite3.SQLITE_CONSTRAINT_PRIMARYKEY, 'SQLITE_CONSTRAINT_PRIMARYKEY'),
])
def test_native_sqlite_cause_code_is_not_guessed_from_message(sql, code, name):
    with sqlite3.connect(':memory:') as conn:
        conn.execute('create table unique_values (id integer primary key)')
        conn.execute('insert into unique_values values (1)')
        with pytest.raises(sqlite3.Error) as caught:
            conn.execute(sql)
    wrapped = RuntimeError('SQLite disk full 13')
    wrapped.__cause__ = caught.value
    facts = environment_cause_fields(wrapped)
    assert facts.get('sqlite_error_code') == code
    assert facts.get('sqlite_error_name') == name
    assert facts['cause_type'] == type(caught.value).__name__
    assert environment_cause_fields(RuntimeError('SQLITE_BUSY 5 database is locked')) == {}
    implicit = RuntimeError('failure')
    implicit.__context__ = caught.value
    assert environment_cause_fields(implicit) == {}


@pytest.fixture
def work_environment(tmp_path, monkeypatch):
    workspace, scratch = tmp_path / 'business', tmp_path / 'empty-scratch'
    workspace.mkdir()
    scratch.mkdir()
    (workspace / 'data.txt').write_text('business input', encoding='utf-8')
    data = {'schema_version': 'script-work-context.v1', 'workspace_root': str(workspace),
            'scratch_dir': str(scratch), 'run_id': 'run', 'task_id': 'task',
            'effective_path_scope': {'read_roots': [str(workspace)], 'write_roots': [str(workspace / 'output')]}}
    monkeypatch.delenv('MY_AGENT_SCRIPT_WORK_CONTEXT_FILE', raising=False)
    monkeypatch.setenv('MY_AGENT_SCRIPT_WORK_CONTEXT_JSON', json.dumps(data))
    monkeypatch.chdir(scratch)
    return data, workspace, scratch


def test_work_context_uses_workspace_not_empty_scratch_cwd(work_environment):
    assert hasattr(runtime_sdk, 'get_work_context')
    data, workspace, scratch = work_environment
    context = runtime_sdk.get_work_context()
    assert isinstance(context, models.ScriptWorkContext)
    assert context.workspace_root == workspace and context.scratch_dir == scratch
    assert context.run_id == data['run_id'] and context.task_id == data['task_id']
    assert runtime_sdk.resolve_workspace_path('data.txt').read_text() == 'business input'
    assert runtime_sdk.resolve_workspace_path('output/result.json', write=True) == workspace / 'output/result.json'
    with pytest.raises(TypeError):
        context.effective_path_scope['write_roots'] = (workspace,)


@pytest.mark.parametrize('relative,write', [('../outside', False), ('/etc/passwd', False),
                                         ('C:\\outside.txt', False), ('data.txt', True)])
def test_sdk_path_escape_and_write_scope_rejected(work_environment, relative, write):
    assert hasattr(runtime_sdk, 'resolve_workspace_path')
    with pytest.raises(ValueError) as caught:
        runtime_sdk.resolve_workspace_path(relative, write=write)
    assert caught.value.error_code in {'SCRIPT_WORKSPACE_PATH_INVALID', 'PATH_OUTSIDE_WORKSPACE', 'PATH_SCOPE_VIOLATION'}


def test_sdk_symlink_escape_rejected(work_environment, tmp_path):
    assert hasattr(runtime_sdk, 'resolve_workspace_path')
    _, workspace, _ = work_environment
    (workspace / 'escape').symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(ValueError) as caught:
        runtime_sdk.resolve_workspace_path('escape/outside')
    assert caught.value.error_code == 'PATH_OUTSIDE_WORKSPACE'


def test_work_context_missing_invalid_and_frozen_file(tmp_path, monkeypatch):
    assert hasattr(runtime_sdk, 'get_work_context')
    for name in ('MY_AGENT_SCRIPT_WORK_CONTEXT_JSON', 'MY_AGENT_SCRIPT_WORK_CONTEXT_FILE'):
        monkeypatch.delenv(name, raising=False)
    with pytest.raises(ValueError) as caught:
        runtime_sdk.get_work_context()
    assert caught.value.error_code == 'SCRIPT_WORK_CONTEXT_MISSING'
    monkeypatch.setenv('MY_AGENT_SCRIPT_WORK_CONTEXT_JSON', '{invalid')
    with pytest.raises(ValueError) as caught:
        runtime_sdk.get_work_context()
    assert caught.value.error_code == 'SCRIPT_WORK_CONTEXT_INVALID'
    monkeypatch.delenv('MY_AGENT_SCRIPT_WORK_CONTEXT_JSON')
    frozen = tmp_path / 'context.json'
    frozen.write_text(json.dumps({'schema_version': 'script-work-context.v1', 'workspace_root': str(tmp_path),
                                 'scratch_dir': str(tmp_path), 'effective_path_scope': {'read_roots': [], 'write_roots': []}}))
    monkeypatch.setenv('MY_AGENT_SCRIPT_WORK_CONTEXT_FILE', str(frozen))
    assert runtime_sdk.get_work_context().workspace_root == tmp_path
    with pytest.raises(ValueError) as caught:
        runtime_sdk.resolve_workspace_path('anything')
    assert caught.value.error_code == 'PATH_SCOPE_VIOLATION'


def test_delivery_intents_are_local_requested_and_explicitly_versioned():
    assert hasattr(runtime_sdk, 'emit_delivery_request')
    first = runtime_sdk.emit_delivery_request('done', ('artifact:1',), request_key='result')
    second = runtime_sdk.emit_delivery_request('done', ('artifact:1',), request_key='result')
    assert first == second and first.status == 'requested'
    assert first.output_refs == ('artifact:1',)
    assert models.SCRIPT_OUTPUT_PROTOCOL_VERSION == 'script-output.v1'
    assert json.loads(json.dumps({'protocol_version': models.SCRIPT_OUTPUT_PROTOCOL_VERSION,
                                 'result': {'ok': True}, 'delivery_intents': [asdict(first)]}))['result'] == {'ok': True}
    with pytest.raises(ValueError):
        replace(first, status='sent')


@pytest.mark.parametrize('key,refs', [('', ()), ('   ', ()), ('key', ('/etc/passwd',)),
                                     ('key', ('C:\\secret.txt',)), ('key', ('../secret',))])
def test_delivery_intent_rejects_invalid_keys_and_path_refs(key, refs):
    assert hasattr(runtime_sdk, 'emit_delivery_request')
    with pytest.raises(ValueError) as caught:
        runtime_sdk.emit_delivery_request(output_refs=refs, request_key=key)
    assert caught.value.error_code == 'SCRIPT_DELIVERY_REQUEST_INVALID'


def test_optional_channel_configuration_defaults_and_registry():
    from agent_py_agent.agent.settings.config import AgentConfig, load_config
    from agent_py_agent.agent.settings.parameter_registry import parameter_registry

    expected = {'im_topic_aggregation_enabled': False, 'im_progress_cards_enabled': True}
    assert all(hasattr(AgentConfig(), key) for key in expected)
    packaged = Path(__file__).parents[1] / 'config' / 'agent_config.yaml'
    config = load_config(packaged)
    for key, default in expected.items():
        spec = parameter_registry()[key]
        assert getattr(config, key) is default and getattr(AgentConfig(), key) is default
        assert spec.default is default and spec.description and spec.category == 'IM 通道'


@pytest.mark.parametrize('broad', [False, True])
def test_host_exclusive_sources_are_hidden_for_owner_and_broad_view(tmp_path, broad):
    from agent_py_agent.agent import path_access_policy as policy
    from agent_py_agent.agent.settings.config import AgentConfig

    assert hasattr(policy, 'host_exclusive_paths')
    root = tmp_path / 'canonical-home'
    owner = root / 'owners' / 'local' / 'main'
    owner.mkdir(parents=True)
    source = tmp_path / 'connection-secret'
    source.write_text('test-only source')
    business = owner / 'business.txt'
    business.write_text('allowed')
    paths = SimpleNamespace(root=root, owner_home_dir=owner)
    config = AgentConfig(feishu_app_secret='file:' + str(source), config_path=str(tmp_path / 'host.yaml'))
    exclusive = policy.host_exclusive_paths(paths, config)
    assert source in exclusive and Path(config.config_path) in exclusive and root / 'config' in exclusive
    access = policy.PathAccessPolicy.from_config(config, agent_home_root=root, owner_scope_root=None if broad else owner)
    for target in (source, root / 'config' / 'desktop.yaml', Path(config.config_path)):
        assert access.check(target).allowed is False
    assert access.check(business).allowed is True
    sandbox = policy.host_sandbox_paths(root, None if broad else owner, config=config)
    assert set(exclusive) <= set(sandbox.hidden)


def test_public_error_codes_are_registered_and_do_not_authorize_replay():
    from agent_py_agent.agent.contracts.error_taxonomy import error_contract

    for code in ('RECOVERY_UNKNOWN', 'RECOVERY_AUTHORITY_UNREADABLE', 'RECOVERY_SOURCE_REVISION_CONFLICT',
                 'SCHEDULER_DEFINITION_GENERATION_CONFLICT', 'SCRIPT_WORK_CONTEXT_MISSING',
                 'SCRIPT_WORK_CONTEXT_INVALID', 'SCRIPT_DELIVERY_REQUEST_INVALID'):
        contract = error_contract(code)
        assert contract.code == code and contract.retryable is False
    assert error_contract('MEMORY_VERSION_CONFLICT').code == 'MEMORY_VERSION_CONFLICT'


def test_file_secret_source_survives_resolved_config_copy_and_stays_host_only(tmp_path):
    from agent_py_agent.agent.path_access_policy import host_exclusive_paths
    from agent_py_agent.agent.settings.config import AgentConfig
    from agent_py_agent.agent.settings.parameter_registry import parameter_registry

    source = tmp_path / 'source'
    config = AgentConfig(feishu_app_secret='file:' + str(source))
    resolved = replace(config, feishu_app_secret='resolved-test-value')
    assert source in host_exclusive_paths(tmp_path / 'canonical-root', resolved)
    assert 'host_exclusive_path_roots' not in parameter_registry()


def test_core_public_work_context_and_optional_registry_transport(tmp_path, monkeypatch):
    from agent_py_agent.agent import core
    from agent_py_agent.agent.settings.config import AgentConfig

    assert hasattr(core, 'build_script_work_context')
    assert hasattr(core, '_tool_registry_host_exclusive_kwargs')
    workspace, scratch = tmp_path / 'workspace', tmp_path / 'scratch'
    agent = SimpleNamespace(effective_workspace_root=workspace)
    context = core.build_script_work_context(agent, scratch_dir=scratch, run_id='run', task_id='task',
        effective_path_scope={'read_roots': [workspace], 'write_roots': []})
    assert context.workspace_root == workspace and context.scratch_dir == scratch and context.run_id == 'run'
    paths = SimpleNamespace(root=tmp_path / 'home')
    old = core.ToolRegistryParams
    monkeypatch.setattr(core, 'ToolRegistryParams', SimpleNamespace(__dataclass_fields__={}))
    assert core._tool_registry_host_exclusive_kwargs(paths, AgentConfig()) == {}
    monkeypatch.setattr(core, 'ToolRegistryParams', SimpleNamespace(__dataclass_fields__={'host_exclusive_paths': None}))
    assert paths.root / 'config' in core._tool_registry_host_exclusive_kwargs(paths, AgentConfig())['host_exclusive_paths']
    monkeypatch.setattr(core, 'ToolRegistryParams', old)


@pytest.mark.parametrize('reason,code', [('unknown_run_status', 'RECOVERY_RUN_UNKNOWN'),
                                       ('attempt_unknown_terminal', 'RECOVERY_ATTEMPT_UNKNOWN')])
def test_recovery_uses_actual_repository_legacy_reason(reason, code):
    from agent_py_agent.agent.agent_core.runtime.loop_models import RecoveryEligibility

    assert RecoveryEligibility.from_block({'reason': reason, 'notice': 'ignored'}).reason_code == code


@pytest.mark.parametrize('same_identity', [False, True])
def test_host_source_case_variants_follow_file_identity_not_global_casefold(tmp_path, monkeypatch, same_identity):
    from agent_py_agent.agent.path_access_policy import PathAccessPolicy

    source, business = tmp_path / 'Config.cfg', tmp_path / 'config.cfg'
    # 注入文件系统身份观察，覆盖敏感卷的两个文件与不敏感卷的同一别名；不冒称运行过 Linux。
    monkeypatch.setattr(Path, 'samefile', lambda self, other: same_identity)
    access = PathAccessPolicy.from_values(mode='full', agent_home_root=tmp_path / 'home', host_exclusive_roots=(source,))
    assert access.check(source).allowed is False
    assert access.check(business).allowed is (not same_identity)


def test_work_context_json_projection_and_public_reply_policy(work_environment):
    from agent_py_agent.agent.conversation import channels

    context = runtime_sdk.get_work_context()
    assert hasattr(context, 'to_dict')
    frozen = json.loads(json.dumps(context.to_dict()))
    assert frozen['schema_version'] == 'script-work-context.v1'
    assert frozen['workspace_root'] == str(context.workspace_root)
    assert set(frozen['effective_path_scope']) == {'read_roots', 'write_roots'}
    assert hasattr(channels, 'DeliveryReplyPolicy')
    assert channels.DeliveryReplyPolicy.REFERENCE.value == 'reference'
    assert channels.DeliveryReplyPolicy.THREAD.value == 'thread'
    assert DeliveryContext('feishu', 'target').reply_policy == ''
