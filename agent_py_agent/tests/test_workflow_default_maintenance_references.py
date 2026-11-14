"""默认Owner维护必须接真实引用读口；不能靠测试注入的完整引用替身证明生产回收。"""
import json
import time
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.user_space.home_layout import ensure_my_agent_home
from agent_py_agent.agent.user_space.owner_maintenance import run_owner_workflow_maintenance
from agent_py_agent.agent.user_space.owner_resolver import ensure_owner_home, home_paths_with_owner
from agent_py_agent.agent.workflows.business_scope import workflow_state
from agent_py_agent.agent.workflows.reference_index import discover
from agent_py_agent.agent.workflows.reference_sources import WorkflowManagementService


@pytest.mark.parametrize('reference', ['none', 'real_subworkflow', 'unreadable_source'])
def test_default_maintenance_uses_real_owner_reference_coverage(tmp_path, reference):
    base = ensure_my_agent_home(tmp_path / 'home')
    home = home_paths_with_owner(base, ensure_owner_home(base.root))
    now = time.time()
    actor = SimpleNamespace(owner_id=home.owner_id)
    api = WorkflowManagementService(home.owner_home_dir, owner_id=home.owner_id, clock=lambda: now - 40 * 86400)

    def create(ref, steps=(), scope='unassigned'):
        return api.execute('workflows.create', {'definition': {'descriptor': {
            'id': ref, 'kind': 'workflow', 'name': ref, 'access_targets': [], 'data_sources': [],
            'credential_refs': [], 'io_contract': {}}, 'steps': list(steps)},
            'scope': {'scope_mode': scope, 'space_ids': []}}, actor=actor, idempotency_key='create-' + ref)

    create('expired')
    if reference != 'none':
        create('real-parent', [{'step_id': 'child', 'name': '真实引用', 'mode': 'subworkflow',
                               'subworkflow_ref': 'workflow:expired'}], scope='owner_global')
    assert discover(api.store, limit=128)['complete']
    source = home.owner_home_dir / 'workflows/real-parent/workflow.json'
    if reference == 'unreadable_source':
        source.write_text('{malformed synthetic source')
    original = api.store.get_workflow('expired')
    result = run_owner_workflow_maintenance(home, {}, now=now, safe_to_delete=True)
    current = api.store.get_workflow('expired')
    if reference == 'none':
        assert result['executed'] and result['counts']['recycled'] == 1
        assert workflow_state(current)['lifecycle'] == 'recycled'
        assert current.descriptor.revision == original.descriptor.revision + 1
    else:
        assert result['counts']['recycled'] == 0
        assert workflow_state(current)['lifecycle'] == 'active'
        assert current.descriptor.revision == original.descriptor.revision
        if reference == 'unreadable_source':
            assert result['counts']['unknown'] >= 1
        else:
            assert result['counts']['skipped'] >= 1
    assert current.execution_definition_ref == original.execution_definition_ref
    # 同一次宿主拍的操作键只能重放原回执，不能对同源重复回收/递增修订。
    assert run_owner_workflow_maintenance(home, {}, now=now, safe_to_delete=True) == result
    assert api.store.get_workflow('expired').descriptor.revision == current.descriptor.revision
    ledger = json.loads((home.owner_data_dir / 'workflow_maintenance.json').read_text())
    assert ledger['inflight'] is None


def test_receipt_write_error_reports_unknown_and_resumes_original_operation(tmp_path, monkeypatch):
    from agent_py_agent.agent.workflows import orphan_maintenance

    base = ensure_my_agent_home(tmp_path / 'home')
    home = home_paths_with_owner(base, ensure_owner_home(base.root))
    now = time.time()
    api = WorkflowManagementService(home.owner_home_dir, owner_id=home.owner_id, clock=lambda: now - 40 * 86400)
    api.execute('workflows.create', {'definition': {'descriptor': {
        'id': 'expired', 'kind': 'workflow', 'name': 'expired', 'access_targets': [],
        'data_sources': [], 'credential_refs': [], 'io_contract': {}}, 'steps': []},
        'scope': {'scope_mode': 'unassigned', 'space_ids': []}},
        actor=SimpleNamespace(owner_id=home.owner_id), idempotency_key='create-expired')
    assert discover(api.store, limit=128)['complete']
    save = orphan_maintenance._save
    failed = False

    def fail_final_receipt(store, record):
        nonlocal failed
        if not failed and record['inflight'] is None and any(
            row['result'].get('counts', {}).get('recycled', 0) for row in record['operations'].values()
        ):
            failed = True
            raise OSError(5, 'private customer token=secret', '/private/customer/secret.json')
        return save(store, record)

    monkeypatch.setattr(orphan_maintenance, '_save', fail_final_receipt)
    result = run_owner_workflow_maintenance(home, {}, now=now, safe_to_delete=True)
    assert failed and result['effect_outcome'] == 'unknown' and result['partial']
    assert result['error_code'] == 'WORKFLOW_MAINTENANCE_UNAVAILABLE'
    assert result['error']['error_type'] == 'OSError' and result['error']['errno'] == 5
    assert 'secret' not in json.dumps(result) and '/private/customer' not in json.dumps(result)
    assert result['reconciliation_source'] == 'data/workflow_maintenance.json'
    assert 'inflight' in result['next_action']
    ledger = json.loads((home.owner_data_dir / 'workflow_maintenance.json').read_text())
    identity = ledger['inflight']['identity']
    assert api.store.get_workflow('expired').descriptor.revision == 2
    resumed = run_owner_workflow_maintenance(home, {'last_attempt_at': now}, now=now + 86400, safe_to_delete=True)
    assert resumed['operation_id'] == identity and resumed['counts']['recycled'] == 1
    assert api.store.get_workflow('expired').descriptor.revision == 2


@pytest.mark.parametrize('exception', [ValueError('private token=secret'), RuntimeError('/private/customer/secret')])
def test_maintenance_state_error_never_echoes_exception_payload(tmp_path, monkeypatch, exception):
    from agent_py_agent.agent.workflows import orphan_maintenance

    base = ensure_my_agent_home(tmp_path / 'home')
    home = home_paths_with_owner(base, ensure_owner_home(base.root))
    (home.owner_data_dir / 'workflow_maintenance.json').write_text('{}')

    def fail(*args, **kwargs):
        raise exception

    monkeypatch.setattr(orphan_maintenance, 'maintain_owner_workflows', fail)
    result = run_owner_workflow_maintenance(home, {}, now=time.time(), safe_to_delete=True)
    assert result['error']['error_type'] == type(exception).__name__
    assert result['effect_outcome'] == 'unknown'
    assert 'secret' not in json.dumps(result) and '/private/customer' not in json.dumps(result)
