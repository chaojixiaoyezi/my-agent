"""新登记必填输出严格校验；旧冻结及未登记定义保留v1并给可观察警告。"""
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.assets.descriptor import (
    WORKFLOW_OUTPUT_CONTRACT_VERSION_KEY as KEY,
)
from agent_py_agent.agent.assets.descriptor import (
    execution_definition_ref,
)
from agent_py_agent.agent.assets.revision_history import get_version
from agent_py_agent.agent.assets.workflow_store import WorkflowDefinition, WorkflowStore
from agent_py_agent.agent.workflows.business_scope import WorkflowScopeService
from agent_py_agent.agent.workflows.engine import WorkflowEngine
from agent_py_agent.agent.workflows.pinned_definitions import resolve_definition
from agent_py_agent.tests.test_business_v12_background_scheduling import (  # noqa: F401
    call,
    native,
)


def definition(outputs=('required',), metadata=None):
    return {'descriptor': {'id': 'output-version', 'kind': 'workflow', 'name': 'output-version',
        'access_targets': [], 'data_sources': [], 'credential_refs': [],
        'io_contract': {'outputs': list(outputs)}, 'metadata': metadata or {}},
        'steps': [{'step_id': 'one', 'name': 'one', 'mode': 'llm_skill_analysis', 'on_error': 'fail'}]}


def run(home, value, output):
    engine = WorkflowEngine(home, analysis_handler=lambda *_: output, run_authorizer=lambda *_: True)
    record = engine.run_workflow(value)
    return record, engine.store.load_run(record.workflow_run_id)


@pytest.mark.parametrize('entry', ['native', 'business'])
@pytest.mark.parametrize('output,status', [({'unrelated': 1}, 'failed'), ({'required': 1}, 'succeeded')])
def test_formal_registration_enforces_untyped_declared_keys(tmp_path, entry, output, status):
    if entry == 'native':
        saved = WorkflowStore(tmp_path).save_workflow(definition())
    else:
        actor = SimpleNamespace(owner_id='owner')
        api = WorkflowScopeService(tmp_path, owner_id='owner', authorize=lambda *_: True)
        api.execute('workflows.create', {'definition': definition()}, actor=actor, idempotency_key='create')
        saved = api._get('output-version')
    assert saved.descriptor.metadata[KEY] == 2
    record, persisted = run(tmp_path, saved.to_dict(), output)
    assert record.status == persisted.status == status
    assert record.final_output == output
    if status == 'failed':
        assert record.breakpoint['error_code'] == 'WORKFLOW_OUTPUT_CONTRACT_VIOLATION'
        details = record.breakpoint['output_contract_validation']
        assert details['missing_keys'] == ['required'] and details['contract_version'] == 2


def test_legacy_unregistered_ref_is_exact_and_warning_is_persisted(tmp_path):
    value = WorkflowDefinition.from_any(definition())
    assert value.execution_definition_ref == '92fa663bfcba83642a7d028787f896e9bab208e5f8e8b569fd13f1968c53af03'
    before = value.to_dict()
    record, persisted = run(tmp_path, before, {'unrelated': 1})
    assert record.status == 'succeeded'
    warning = persisted.steps[-1].escalation_result['output_contract_warning']
    assert warning['code'] == 'WORKFLOW_LEGACY_OUTPUT_CONTRACT_UNVERIFIED'
    assert warning['missing_keys'] == ['required'] and warning['executed_step_count'] == 1
    assert value.to_dict() == before


@pytest.mark.parametrize('entry', ['native', 'business'])
def test_explicit_empty_and_removing_last_output_do_not_add_constraints(tmp_path, entry):
    actor = SimpleNamespace(owner_id='owner')
    api = WorkflowScopeService(tmp_path, owner_id='owner', authorize=lambda *_: True)
    store = api.store
    if entry == 'native':
        first = store.save_workflow(definition())
        removed = store.save_workflow(definition(()), expected_revision=first.descriptor.revision)
    else:
        api.execute('workflows.create', {'definition': definition()}, actor=actor, idempotency_key='create')
        api.execute('workflows.update', {'workflow_ref': 'output-version', 'definition': definition(())},
            actor=actor, expected_revision=1, idempotency_key='remove-last')
        removed = api._get('output-version')
    assert removed.descriptor.metadata[KEY] == 2
    assert run(tmp_path, removed.to_dict(), {'unrelated': 1})[0].status == 'succeeded'
    # 首次显式空声明不需要新版本，仍然没有必填输出。
    empty_home = tmp_path / 'empty'
    empty = WorkflowStore(empty_home).save_workflow(definition(()))
    assert KEY not in empty.descriptor.metadata
    assert run(empty_home, empty.to_dict(), {'unrelated': 1})[0].status == 'succeeded'


@pytest.mark.parametrize('entry', ['native', 'business'])
def test_input_downgrade_cannot_disable_formal_required_output(tmp_path, entry):
    raw = definition(metadata={KEY: 1})
    if entry == 'native':
        store = WorkflowStore(tmp_path)
        saved = store.save_workflow(raw)
        saved = store.save_workflow(raw, expected_revision=saved.descriptor.revision)
    else:
        actor = SimpleNamespace(owner_id='owner')
        api = WorkflowScopeService(tmp_path, owner_id='owner', authorize=lambda *_: True)
        api.execute('workflows.create', {'definition': raw}, actor=actor, idempotency_key='create')
        api.execute('workflows.update', {'workflow_ref': 'output-version', 'definition': raw},
            actor=actor, expected_revision=1, idempotency_key='downgrade')
        saved = api._get('output-version')
    assert saved.descriptor.metadata[KEY] == 2
    assert run(tmp_path, saved.to_dict(), {'unrelated': 1})[0].status == 'failed'


def test_existing_frozen_version_and_history_pin_are_not_upgraded(tmp_path):
    store = WorkflowStore(tmp_path)
    old = WorkflowDefinition.from_any(definition())
    with store._locked():
        legacy = store._save(old)  # fixture：已有v1资产，绕过新的正式登记边界。
    old_version = get_version(tmp_path, 'workflow', 'output-version', 1)
    old_ref = execution_definition_ref(legacy.to_dict(), kind='workflow')
    frozen, _ = resolve_definition(tmp_path, 'output-version', 1)
    current = store.save_workflow(definition(), expected_revision=legacy.descriptor.revision)
    assert current.descriptor.metadata[KEY] == 2
    assert execution_definition_ref(current.to_dict(), kind='workflow') != old_ref
    again, _ = resolve_definition(tmp_path, 'output-version', 1)
    assert frozen == again and execution_definition_ref(again, kind='workflow') == old_ref
    assert get_version(tmp_path, 'workflow', 'output-version', 1) == old_version
    assert run(tmp_path, frozen, {'unrelated': 1})[0].status == 'succeeded'
    assert run(tmp_path, current.to_dict(), {'unrelated': 1})[0].status == 'failed'


def test_invalid_version_is_rejected_and_scripts_hash_ignores_workflow_metadata(tmp_path):
    with pytest.raises(ValueError, match='WORKFLOW_OUTPUT_CONTRACT_VERSION_INVALID'):
        WorkflowStore(tmp_path).save_workflow(definition(metadata={KEY: '2'}))
    script = {'descriptor': {'id': 'script', 'kind': 'script', 'metadata': {}}, 'code': 'pass'}
    changed = {**script, 'descriptor': {**script['descriptor'], 'metadata': {KEY: 2}}}
    assert execution_definition_ref(script, kind='script') == execution_definition_ref(changed, kind='script')


def test_successful_prefix_supplies_required_key_without_rewriting_final_output(tmp_path):
    value = definition()
    value['steps'].append({**value['steps'][0], 'step_id': 'last'})
    saved = WorkflowStore(tmp_path).save_workflow(value)
    engine = WorkflowEngine(tmp_path, analysis_handler=lambda spec, *_:
        {'required': 1} if spec['step_id'] == 'one' else {'unrelated': 1})
    result = engine.run_workflow(saved.to_dict())
    assert result.status == 'succeeded' and result.final_output == {'unrelated': 1}


@pytest.mark.parametrize('nullable,status', [(False, 'failed'), (True, 'succeeded')])
def test_v2_reuses_existing_nullable_output_contract(tmp_path, nullable, status):
    value = definition()
    if nullable:
        value['descriptor']['io_contract']['nullable_outputs'] = ['required']
    saved = WorkflowStore(tmp_path).save_workflow(value)
    result, _ = run(tmp_path, saved.to_dict(), {'required': None})
    assert result.status == status
    if status == 'failed':
        assert result.breakpoint['output_contract_validation']['null_keys'] == ['required']


def test_only_explicit_skill_revision_upgrades_legacy_not_control_or_noop(tmp_path):
    store = WorkflowStore(tmp_path)
    with store._locked():
        store._save(WorkflowDefinition.from_any(definition()))
    store.set_enabled('output-version', False)
    assert KEY not in store.get_workflow('output-version').descriptor.metadata
    store.detach_skill_from_step('output-version', 'one', 'unused')
    assert KEY not in store.get_workflow('output-version').descriptor.metadata
    changed = store.attach_skill_to_step('output-version', 'one', 'new-skill')
    assert changed.descriptor.metadata[KEY] == 2


def test_old_paused_run_resumes_its_frozen_v1_after_registration_upgrade(tmp_path):
    raw = definition()
    raw['steps'][0]['on_error'] = 'escalate_to_agent'
    store = WorkflowStore(tmp_path)
    with store._locked():
        legacy = store._save(WorkflowDefinition.from_any(raw))

    def unavailable(*_):
        raise ValueError('controlled analysis failure')

    engine = WorkflowEngine(tmp_path, analysis_handler=unavailable)
    paused = engine.run_workflow(legacy.to_dict())
    assert paused.status in ('failed', 'paused_for_resume')
    frozen_before = engine.store.load_workflow_snapshot(paused.workflow_run_id)
    store.save_workflow(raw, expected_revision=legacy.descriptor.revision)
    engine.analysis_handler = lambda *_: {'unrelated': 1}
    resumed = engine.resume_workflow_run(paused.workflow_run_id)
    assert resumed.status == 'succeeded'
    assert resumed.steps[-1].escalation_result['output_contract_warning']['contract_version'] == 1
    assert engine.store.load_workflow_snapshot(paused.workflow_run_id) == frozen_before


def test_business_warning_real_client_server_roundtrip(native):  # noqa: F811
    _, agent = native
    home = agent.home_paths.owner_home_dir
    engine = WorkflowEngine(home, analysis_handler=lambda *_: {'unrelated': 1})
    result = engine.run_workflow(definition(), owner_id=agent.home_paths.owner_id)
    assert result.status == 'succeeded'
    status, envelope = call(native, 'workflow_runs.get', {'workflow_run_id': result.workflow_run_id})
    assert status == 200 and envelope['error'] is None
    warning = envelope['data']['steps'][0]['output_contract_warning']
    assert warning['code'] == 'WORKFLOW_LEGACY_OUTPUT_CONTRACT_UNVERIFIED'
    assert warning['missing_keys'] == ['required'] and warning['contract_version'] == 1
    # 同样走真实路由，输出符合声明的旧运行不能被补造一个warning。
    engine.analysis_handler = lambda *_: {'required': 1}
    valid = engine.run_workflow(definition(), owner_id=agent.home_paths.owner_id)
    status, envelope = call(native, 'workflow_runs.get', {'workflow_run_id': valid.workflow_run_id})
    assert status == 200 and 'output_contract_warning' not in envelope['data']['steps'][0]


def test_public_warning_redacts_missing_keys_without_inventing_old_state(tmp_path):
    from agent_py_agent.agent.workflows.business_scope import _public_output_contract_warning

    result, _ = run(tmp_path, definition(), {'unrelated': 1})
    secret = 'sk-proj-' + 'a' * 48
    result.steps[0].escalation_result['output_contract_warning']['missing_keys'] = [secret]
    projected = _public_output_contract_warning(result.steps[0])
    assert secret not in str(projected)
    assert projected['output_contract_warning']['missing_keys']
    assert _public_output_contract_warning(SimpleNamespace(escalation_result={})) == {}
