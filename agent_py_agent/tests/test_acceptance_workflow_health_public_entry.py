"""WF-11：原Store只读预算、生产Client回环及原实时授权边界。"""
from __future__ import annotations

import hashlib
import json
import os

import pytest

from agent_py_agent.agent.workflows.run_store import WorkflowRunStore
from agent_py_agent.tests.test_acceptance_workflow_native_ports import (
    CounterTool,
    flow,
    host,
    wire,
)


def snapshot(root):
    return {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in root.rglob('*') if p.is_file()}


def raw_run(home, identity, *, workflow='sample', owner='local/main', **changes):
    from agent_py_agent.agent.workflows.models import WorkflowRunRecord

    run = WorkflowRunRecord(workflow_run_id=identity, workflow_id=workflow, owner_id=owner,
        status='succeeded', steps=(), trigger_kind='manual', started_at=1.0, ended_at=2.0,
        duration_ms=1000.0, **changes)
    folder = home / 'data' / 'workflow_runs' / identity
    folder.mkdir(parents=True)
    (folder / 'run.json').write_text(json.dumps(run.to_dict()), encoding='utf-8')
    return run


def test_readonly_sample_missing_and_empty_zero_writes(tmp_path):
    home = tmp_path / 'owner'
    home.mkdir()
    store = WorkflowRunStore(home)
    before = snapshot(home)
    result = store.sample_runs_readonly(workflow_id='sample', limit=3)
    assert result['runs'] == [] and result['coverage'] == 'complete'
    assert result['scan_count'] == result['read_count'] == 0
    assert snapshot(home) == before


def test_readonly_sample_actual_enumeration_reads_bytes_bounded(tmp_path, monkeypatch):
    home = tmp_path / 'owner'
    home.mkdir()
    for n in range(31):
        raw_run(home, f'r{n}', workflow='other' if n % 2 else 'sample')
    before = snapshot(home)
    counts = {'entries': 0, 'bytes': 0, 'reads': 0}
    scandir, read = os.scandir, os.read

    class Entries:
        def __init__(self, source):
            self.source = scandir(source)
        def __enter__(self):
            return self
        def __exit__(self, *args):
            self.source.close()
        def __iter__(self):
            return self
        def __next__(self):
            entry = next(self.source)
            counts['entries'] += 1
            return entry

    def counted_read(fd, size):
        value = read(fd, size)
        counts['bytes'] += len(value)
        counts['reads'] += bool(value)
        return value

    with monkeypatch.context() as patch:
        patch.setattr(os, 'scandir', Entries)
        patch.setattr(os, 'read', counted_read)
        result = WorkflowRunStore(home).sample_runs_readonly(workflow_id='sample', limit=2)
    assert result['coverage'] == 'partial' and result['truncated'] is True
    assert counts['entries'] == result['scan_count'] <= result['scan_limit']
    assert result['read_count'] <= result['read_limit']
    assert counts['bytes'] <= result['read_limit'] * (result['max_file_bytes'] + 1)
    assert len(result['runs']) <= 2
    assert all(r.workflow_id == 'sample' for r in result['runs'])
    assert snapshot(home) == before


@pytest.mark.parametrize('bad', [True, 1.0, 0, 101, '3', None])
def test_sample_rejects_non_strict_limit(tmp_path, bad):
    with pytest.raises(ValueError):
        WorkflowRunStore(tmp_path).sample_runs_readonly(workflow_id='sample', limit=bad)


def test_sample_oversize_corrupt_and_symlink_not_complete(tmp_path):
    home = tmp_path / 'owner'
    home.mkdir()
    raw_run(home, 'large')
    path = home / 'data/workflow_runs/large/run.json'
    path.write_bytes(b'x' * (2 * 1024 * 1024))
    before = snapshot(home)
    result = WorkflowRunStore(home).sample_runs_readonly(workflow_id='sample', limit=2)
    assert result['coverage'] in ('partial', 'unknown')
    assert result['runs'] == [] and result['read_count'] == 1
    assert result['errors']
    assert snapshot(home) == before
    path.write_text('{broken', encoding='utf-8')
    assert WorkflowRunStore(home).sample_runs_readonly(workflow_id='sample', limit=2)['coverage'] != 'complete'
    path.unlink()
    path.symlink_to(tmp_path / 'outside')
    assert WorkflowRunStore(home).sample_runs_readonly(workflow_id='sample', limit=2)['coverage'] != 'complete'


def test_public_health_wire_normal_failure_attempt_and_old_get(wire):
    agent, client = wire
    tool = CounterTool()
    agent.tools.register(tool)
    definition = flow('health-wire', [{'step_id': 'count', 'name': 'count', 'mode': 'tool_call',
        'on_error': 'fail', 'tool_spec': {'tool_name': 'native_counter', 'arguments': {'value': -1}}}])
    assert client.call('workflows.create', {'definition': definition})['ok']
    assert client.call('workflows.run', {'workflow_ref': 'health-wire'})['ok']
    store = WorkflowRunStore(agent.home_paths.owner_home_dir)
    [run] = store.list_runs(limit=10)
    assert run.status == 'failed' and run.steps[0].error_code == 'TOOL_ERROR'
    old = client.call('workflow_runs.get', {'workflow_run_id': run.workflow_run_id})
    before = snapshot(store.root)
    result = client.call('workflows.health.get', {'workflow_ref': 'health-wire'})
    assert result['ok'], result
    data = result['data']
    assert data['workflow_ref'] == 'health-wire'
    assert data['health']['sampled_runs'] == 1
    assert data['health']['bounded_limit'] == 50
    assert data['health']['reason_classes']['TOOL_ERROR'] == 1
    assert data['health']['retry_stats']['total_attempts'] == run.steps[0].attempt_count
    assert data['health']['step_health']['count']['duration_ms']['count'] == 1
    assert data['health_read_coverage']['read_count'] == 1
    assert snapshot(store.root) == before
    assert client.call('workflow_runs.get', {'workflow_run_id': run.workflow_run_id})['data'] == old['data']
    assert set(old['data']['run']) == {'workflow_run_id', 'workflow_ref', 'task_id', 'space_id',
        'status', 'execution_definition_ref', 'scope_revision', 'phase', 'effect_outcome',
        'confirmed_step_refs', 'revision'}


@pytest.mark.parametrize('params', [{}, {'workflow_ref': 'x', 'limit': True},
    {'workflow_ref': 'x', 'limit': 1.0}, {'workflow_ref': 'x', 'limit': 101},
    {'workflow_ref': 'x', 'limit': 0}, {'workflow_ref': 'x', 'owner': 'other'},
    {'workflow_ref': 'x', 'redact': False}])
def test_public_health_schema_before_io(wire, params, monkeypatch):
    from agent_py_agent.agent.gateway_parts.business_api_v13 import v13_validate_params
    from agent_py_agent.agent.plugin_host.errors import HostAPIError

    _, client = wire
    def forbidden(*args, **kwargs):
        pytest.fail('schema-invalid request reached run store')
    monkeypatch.setattr(WorkflowRunStore, 'sample_runs_readonly', forbidden)
    # 生产Client本身也使用规范schema；不绕过它发送手写假信封。
    with pytest.raises(HostAPIError) as invalid:
        v13_validate_params('workflows.health.get', params)
    assert invalid.value.code == 'INVALID_SCHEMA'
    with pytest.raises((ValueError, RuntimeError)):
        client.prepare('workflows.health.get', params)


def test_health_domain_owner_mismatch_and_live_revoke(host, monkeypatch):
    from agent_py_agent.agent.gateway_parts.business_api import local_business_context

    context, services = local_business_context(host, model=True)
    actor = services.actor(context)
    assert services.workflows.execute('workflows.create', {'definition': flow('sample', [])},
        actor=actor, idempotency_key='create-health-sample')
    home = host.home_paths.owner_home_dir
    raw_run(home, 'wrong-owner', owner='someone/else')
    with pytest.raises(PermissionError):
        services.workflows.health_get({'workflow_ref': 'sample'}, actor=actor)
    # 不替换actor/authorizer；原模型上下文的request绑定变化必须即时撤权。
    home2 = home / 'data/workflow_runs/wrong-owner/run.json'
    home2.write_text(home2.read_text().replace('someone/else', 'local/main'))
    original = WorkflowRunStore.sample_runs_readonly
    def revoked(store, **kwargs):
        result = original(store, **kwargs)
        host._current_run_params = object()
        return result
    monkeypatch.setattr(WorkflowRunStore, 'sample_runs_readonly', revoked)
    with pytest.raises(PermissionError):
        services.workflows.health_get({'workflow_ref': 'sample'}, actor=actor)


def test_health_feature_disabled_no_sample(wire, monkeypatch):
    agent, client = wire
    agent.config.business_spaces_enabled = False
    def forbidden(*args, **kwargs):
        pytest.fail('disabled feature read historical runs')
    monkeypatch.setattr(WorkflowRunStore, 'sample_runs_readonly', forbidden)
    result = client.call('workflows.health.get', {'workflow_ref': 'x'})
    assert not result['ok'] and result['error']['code'] == 'HOST_NOT_READY', result


def test_helper_dependency_preserves_partial_coverage(wire):
    """入口窗口的partial覆盖必须传到唯一helper；不复制兄弟算法或放松断言。"""
    agent, client = wire
    assert client.call('workflows.create', {'definition': flow('sample', [])})['ok']
    for n in range(60):
        raw_run(agent.home_paths.owner_home_dir, f'unknown-history-{n}', workflow='other')
    result = client.call('workflows.health.get', {'workflow_ref': 'sample', 'limit': 1})
    assert result['ok'], result
    assert result['data']['health_read_coverage']['coverage'] == 'partial'
    assert result['data']['health']['coverage'] in ('partial', 'unknown')


def test_real_service_v13_normal_and_bounded_metadata(host):
    from agent_py_agent.agent.gateway_parts.business_api import local_business_context
    from agent_py_agent.agent.gateway_parts.business_api_v13 import v13_invoke

    context, services = local_business_context(host, model=True)
    actor = services.actor(context)
    services.workflows.execute('workflows.create', {'definition': flow('sample', [])},
        actor=actor, idempotency_key='create-health-sample')
    raw_run(host.home_paths.owner_home_dir, 'original-run')
    before = snapshot(host.home_paths.owner_home_dir / 'data/workflow_runs')
    result = v13_invoke('workflows.health.get', {'workflow_ref': 'sample'}, context, services)
    assert result['health']['run_counts']['succeeded'] == 1
    assert result['health']['sampled_runs'] == 1
    assert result['health_read_coverage']['latest_guaranteed'] is False
    assert result['health_read_coverage']['read_count'] == 1
    assert snapshot(host.home_paths.owner_home_dir / 'data/workflow_runs') == before


def test_real_service_helper_dependency_source_coverage(host):
    from agent_py_agent.agent.gateway_parts.business_api import local_business_context
    from agent_py_agent.agent.gateway_parts.business_api_v13 import v13_invoke

    context, services = local_business_context(host, model=True)
    services.workflows.execute('workflows.create', {'definition': flow('sample', [])},
        actor=services.actor(context), idempotency_key='create-health-sample')
    for n in range(20):
        raw_run(host.home_paths.owner_home_dir, f'other-{n}', workflow='other')
    result = v13_invoke('workflows.health.get', {'workflow_ref': 'sample', 'limit': 1}, context, services)
    assert result['health_read_coverage']['coverage'] == 'partial'
    assert result['health']['coverage'] in ('partial', 'unknown')


def test_actual_scan_budget_with_non_run_entries(tmp_path, monkeypatch):
    root = tmp_path / 'data/workflow_runs'
    root.mkdir(parents=True)
    for n in range(80):
        (root / f'unrelated-{n}').write_text('not a run')
    original, count = os.scandir, []

    class Entries:
        def __init__(self, source):
            self.source = original(source)
        def __enter__(self):
            return self
        def __exit__(self, *args):
            self.source.close()
        def __iter__(self):
            return self
        def __next__(self):
            entry = next(self.source)
            count.append(entry.name)
            return entry

    monkeypatch.setattr(os, 'scandir', Entries)
    result = WorkflowRunStore(tmp_path).sample_runs_readonly(workflow_id='sample', limit=1)
    assert len(count) == result['scan_count'] == result['scan_limit'] == 16
    assert result['read_count'] == 0 and result['coverage'] == 'partial' and result['truncated']


def test_return_rechecks_original_model_context_after_helper(host, monkeypatch):
    from agent_py_agent.agent.gateway_parts.business_api import local_business_context
    from agent_py_agent.agent.workflows import query_health

    context, services = local_business_context(host, model=True)
    actor = services.actor(context)
    services.workflows.execute('workflows.create', {'definition': flow('sample', [])},
        actor=actor, idempotency_key='create-health-sample')
    raw_run(host.home_paths.owner_home_dir, 'original-run')
    original = query_health.build_workflow_health_view
    def revoke(window, **kwargs):
        result = original(window, **kwargs)
        host._current_run_params = object()
        return result
    monkeypatch.setattr(query_health, 'build_workflow_health_view', revoke)
    with pytest.raises(PermissionError):
        services.workflows.health_get({'workflow_ref': 'sample'}, actor=actor)


def test_service_returns_only_requested_workflow_no_reference_reads(host, monkeypatch):
    from agent_py_agent.agent.gateway_parts.business_api import local_business_context

    context, services = local_business_context(host, model=True)
    actor = services.actor(context)
    services.workflows.execute('workflows.create', {'definition': flow('sample', [])},
        actor=actor, idempotency_key='create-health-sample')
    raw_run(host.home_paths.owner_home_dir, 'original-run')
    raw_run(host.home_paths.owner_home_dir, 'other-run', workflow='other')
    def forbidden(*args, **kwargs):
        pytest.fail('health recursively loaded a referenced run or built an engine')
    monkeypatch.setattr(WorkflowRunStore, 'load_run', forbidden)
    monkeypatch.setattr(services.workflows, '_engine', forbidden)
    result = services.workflows.health_get({'workflow_ref': 'sample'}, actor=actor)
    assert set(result['health']['workflow_counts']) == {'sample'}
    assert result['health']['sampled_runs'] == 1


def test_server_wire_observation_does_not_hide_client_gap(wire):
    """真实observer/HTTP服务观察；客户端解析已接线，本项仍不替代其完整权限矩阵。"""
    from agent_py_agent.agent.gateway_parts.business_api_v13 import v13_action_spec
    from agent_py_agent.agent.plugin_host.schemas import validate_object

    agent, client = wire
    counter = CounterTool()
    agent.tools.register(counter)
    definition = flow('server-health', [{'step_id': 'count', 'name': 'count', 'mode': 'tool_call',
        'on_error': 'fail', 'tool_spec': {'tool_name': 'native_counter', 'arguments': {'value': -1}}}])
    assert client.call('workflows.create', {'definition': definition})['ok']
    assert client.call('workflows.run', {'workflow_ref': 'server-health'})['ok']
    [run] = WorkflowRunStore(agent.home_paths.owner_home_dir).list_runs(limit=10)
    old = client.call('workflow_runs.get', {'workflow_run_id': run.workflow_run_id})
    root = agent.home_paths.owner_home_dir / 'data/workflow_runs'
    before = snapshot(root)
    request = validate_object({'action': 'workflows.health.get', 'request_id': 'server-health-probe',
        'params': {'workflow_ref': 'server-health'}},
        v13_action_spec('workflows.health.get').native_request_schema)
    status, body = client.transport('/client/business', request, timeout=10)
    assert status == 200 and body['state'] == 'completed' and body['error'] is None, body
    assert body['data']['health']['reason_classes']['TOOL_ERROR'] == 1
    assert body['data']['health']['retry_stats']['total_attempts'] == run.steps[0].attempt_count
    assert body['data']['health']['sampled_runs'] == 1
    assert snapshot(root) == before
    assert client.call('workflow_runs.get', {'workflow_run_id': run.workflow_run_id})['data'] == old['data']
