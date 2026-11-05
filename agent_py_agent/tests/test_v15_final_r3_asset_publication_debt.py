"""R1 v2: nonfatal W4 saves, content-addressed debt, scripts-first I/O retry."""
import hashlib
import json
import os
from contextlib import contextmanager

import pytest

from agent_py_agent.agent.assets import asset_root_lock, version_hook
from agent_py_agent.agent.assets import revision_history as history
from agent_py_agent.agent.assets.descriptor import AssetDescriptor
from agent_py_agent.agent.assets.script_store import ScriptStore
from agent_py_agent.agent.assets.workflow_store import (
    WorkflowDefinition,
    WorkflowStepSpec,
    WorkflowStore,
)
from agent_py_agent.agent.assets.workspace_assets import commit_asset_batch, promote_to_owner
from agent_py_agent.agent.scheduler.repository import SchedulerJobCreateRequest, SchedulerRepository
from agent_py_agent.agent.tooling.workflows_tool import WorkflowsTool
from agent_py_agent.agent.workflows.pinned_definitions import resolve_definition


def _definition(body='A', *, identifier='flow', script_id='missing'):
    return WorkflowDefinition(AssetDescriptor(identifier, 'workflow', identifier), (
        WorkflowStepSpec('fetch', 'fetch', 'hybrid', script_id=script_id),), body)


def _call(home, **params):
    result = WorkflowsTool(home).execute(params)
    assert result.ok, result.output
    return json.loads(result.output)


def _save(home, definition, *, expected_revision=None):
    return _call(home, action='save', workflow_id=definition.descriptor.id,
        definition=definition.to_dict(), expected_revision=expected_revision)


def _script(home, identifier='demo', code='print(1)\n'):
    return ScriptStore(home).save_script(AssetDescriptor(identifier, 'script', identifier), code)


def _leaves(home, identifier='flow'):
    return sorted((home / 'data/asset_evolution/pending/workflow').glob(f'{identifier}.unpublishable.*.json'))


def _retained(home, proof):
    digest = hashlib.sha256(history._canonical(proof).encode('utf-8')).hexdigest()[:16]
    revision = int(proof['snapshot'].get('storage_revision') or 0)
    path = home / 'data/asset_evolution/pending/workflow' / f'flow.unpublishable.{revision}.{digest}.json'
    value = json.loads(path.read_text())
    assert value['schema_version'] == 'asset_publication_unpublishable.v1'
    assert value['kind'] == 'workflow' and value['asset_id'] == 'flow'
    assert value['error_code'] and value['quarantined_at']
    assert value['publication'] == proof
    assert proof['phase'] == 'committed'
    return path


@contextmanager
def _no_fd_growth():
    directory = '/dev/fd' if os.path.isdir('/dev/fd') else '/proc/self/fd'
    before = len(os.listdir(directory))
    try:
        yield
    finally:
        fd_delta = len(os.listdir(directory)) - before
        assert fd_delta == 0


def _repository(home):
    return SchedulerRepository(home / 'data/scheduler', owner_provider='local', owner_kind='main', owner_id='main')


def _job(repo, name):
    job, reused = repo.create_job(SchedulerJobCreateRequest(name=name, prompt='local bookkeeping',
        thread_id='thread-debt', schedule={'kind': 'every', 'every_seconds': 600, 'anchor_at': 1000},
        source_request_id=name, now=900))
    assert reused is False
    return job


def _batch_item(value):
    return {'descriptor': value.descriptor.to_dict(), 'steps': [step.to_dict() for step in value.steps],
        'body_markdown': value.body_markdown}


def test_forward_reference_revisions_are_retained_not_published(tmp_path):
    proofs = []
    with _no_fd_growth():
        first = _save(tmp_path, _definition())
        assert first['version_recording']['recorded'] is False
        proofs.append(history.pending_publication(tmp_path, 'workflow', 'flow'))
        second = _save(tmp_path, _definition('B'), expected_revision=1)
        assert second['version_recording']['recorded'] is False
        _retained(tmp_path, proofs[-1])
        proofs.append(history.pending_publication(tmp_path, 'workflow', 'flow'))
        bound = _call(tmp_path, action='bind_skill', workflow_id='flow', step_id='fetch', skill_id='skill-B')
        assert bound['version_recording']['recorded'] is False
        _retained(tmp_path, proofs[-1])
        proofs.append(history.pending_publication(tmp_path, 'workflow', 'flow'))
        with version_hook.version_operation() as operation:
            assert WorkflowStore(tmp_path).set_enabled('flow', False).enabled is False
        assert operation['recording']['recorded'] is False
        _retained(tmp_path, proofs[-1])
        proofs.append(history.pending_publication(tmp_path, 'workflow', 'flow'))
        assert all(proof['snapshot']['error_code'] for proof in proofs)
        assert history.latest_version(tmp_path, 'workflow', 'flow') is None
        _script(tmp_path, 'missing')
        saved = _save(tmp_path, _definition('complete'), expected_revision=4)
        assert saved['version_recording']['recorded'] is True
        assert len(_leaves(tmp_path)) == len(proofs)
        raw = {path: path.read_bytes() for path in (_retained(tmp_path, proof) for proof in proofs)}
        current = history.latest_version(tmp_path, 'script', 'missing')
        published = history.latest_version(tmp_path, 'workflow', 'flow')
        assert published['pins']['scripts']['missing'] == {
            key: current[key] for key in ('version', 'code_sha256', 'execution_definition_ref')}
        assert resolve_definition(tmp_path, 'flow', published['version'])
        assert history.pending_publication(tmp_path, 'workflow', 'flow') is None
        assert _call(tmp_path, action='delete', workflow_id='flow')['deleted'] is True
        assert WorkflowStore(tmp_path).get_workflow('flow') is None
        assert all(path.read_bytes() == content for path, content in raw.items())


def test_single_pin_read_error_keeps_w4_commit_and_quarantines_next_edit(tmp_path, monkeypatch):
    _script(tmp_path)
    _save(tmp_path, _definition(script_id='demo'))
    original, calls = history.find_version, []

    def read(home, kind, asset_id, **kwargs):
        if kind == 'script' and not calls:
            calls.append(asset_id)
            raise OSError('one pin read I/O error')
        return original(home, kind, asset_id, **kwargs)

    with _no_fd_growth(), monkeypatch.context() as patch:
        patch.setattr(history, 'find_version', read)
        saved = _save(tmp_path, _definition('B', script_id='demo'), expected_revision=1)
        assert saved['version_recording']['recorded'] is False
        assert calls == ['demo']
        assert WorkflowStore(tmp_path).get_workflow('flow').body_markdown == 'B'
        proof = history.pending_publication(tmp_path, 'workflow', 'flow')
        assert proof['snapshot']['error_code']
    with _no_fd_growth():
        _script(tmp_path, code='print(2)\n')
        saved = _save(tmp_path, _definition('C', script_id='demo'), expected_revision=2)
        assert saved['version_recording']['recorded'] is True
        _retained(tmp_path, proof)
        record = history.latest_version(tmp_path, 'workflow', 'flow')
        assert record['pins']['scripts']['demo']['version'] == 2
        assert history.blob_get(tmp_path, record['side_blobs']['WORKFLOW.md']) == b'C'
        assert history.pending_publication(tmp_path, 'workflow', 'flow') is None


def test_deterministic_failure_preserves_exact_pending_then_allows_mutations(tmp_path, monkeypatch):
    _script(tmp_path)
    original, calls = history.record_version, []

    def record(home, kind, asset_id, **kwargs):
        if kind == 'workflow' and asset_id == 'flow':
            calls.append(kwargs['storage_revision'])
            assert len(calls) <= 6
            raise ValueError('conflicting base workflow pin')
        return original(home, kind, asset_id, **kwargs)

    with _no_fd_growth(), monkeypatch.context() as patch:
        patch.setattr(history, 'record_version', record)
        saved = _save(tmp_path, _definition(script_id='demo'))
        assert saved['version_recording']['recorded'] is False
        assert saved['version_recording'].get('retryable') is False
        first = history.pending_publication(tmp_path, 'workflow', 'flow')
        second = _save(tmp_path, _definition('B', script_id='demo'), expected_revision=1)
        assert second['version_recording']['recorded'] is False
        first_path = _retained(tmp_path, first)
        raw = first_path.read_bytes()
        proof = history.pending_publication(tmp_path, 'workflow', 'flow')
        assert _call(tmp_path, action='delete', workflow_id='flow')['deleted'] is True
        _retained(tmp_path, proof)
        assert first_path.read_bytes() == raw
        assert history.pending_publication(tmp_path, 'workflow', 'flow') is None
        assert WorkflowStore(tmp_path).get_workflow('flow') is None
    with _no_fd_growth():
        assert _save(tmp_path, _definition('C', script_id='demo'))['version_recording']['recorded'] is True
        assert history.latest_version(tmp_path, 'workflow', 'flow')['pins']['scripts']['demo']['version'] == 1
        assert first_path.read_bytes() == raw


@pytest.mark.parametrize('route', ['batch', 'promotion'])
def test_missing_script_batch_releases_root_and_scheduler(tmp_path, route):
    home, workspace = tmp_path / 'home', tmp_path / 'project'
    home.mkdir()
    workspace.mkdir()
    repo = _repository(home)
    first = _job(repo, 'before')
    value = _definition()
    with _no_fd_growth():
        if route == 'promotion':
            WorkflowStore(home, workspace_root=workspace).save_workflow(value, target_scope='workspace')
            result = promote_to_owner('workflow', 'flow', workspace_root=workspace, owner_home=home)
        else:
            result = commit_asset_batch([_batch_item(value)], owner_home=home)
        assert result['committed'] is True
        assert not (home / '.asset-batch.json').exists(), 'nonretryable history debt must release the root journal'
        proof = history.pending_publication(home, 'workflow', 'flow')
        assert proof['phase'] == 'committed' and proof['snapshot']['error_code']
        assert history.latest_version(home, 'workflow', 'flow') is None
        assert repo.get_job(first['job_id'])['name'] == 'before'
        second = _job(_repository(home), 'after')
        jobs, errors = _repository(home).list_jobs()
        assert not errors and {job['job_id'] for job in jobs} == {first['job_id'], second['job_id']}
        _script(home, 'unrelated')
        _script(home, 'missing')
        _save(home, _definition('unrelated', identifier='unrelated', script_id='missing'))
        assert history.pending_publication(home, 'workflow', 'flow') == proof
        assert history.latest_version(home, 'workflow', 'flow') is None
        with asset_root_lock(home):
            pass
        assert _call(home, action='delete', workflow_id='flow')['deleted'] is True
        _retained(home, proof)
        assert history.latest_version(home, 'workflow', 'flow') is None
        if route == 'promotion':
            assert not (workspace / '.agent/data/asset_evolution').exists()


@pytest.mark.parametrize('interrupted', [False, True])
def test_batch_deterministic_debt_keeps_evidence_not_root_journal(tmp_path, monkeypatch, interrupted):
    _script(tmp_path)
    original, calls = history.record_version, []

    def record(home, kind, asset_id, **kwargs):
        if kind == 'workflow' and asset_id == 'flow':
            calls.append(kwargs['storage_revision'])
            assert len(calls) <= 5
            if interrupted and len(calls) == 1:
                raise KeyboardInterrupt()
            raise ValueError('conflicting base workflow pin')
        return original(home, kind, asset_id, **kwargs)

    items = [_batch_item(_definition('B', script_id='demo')),
             _batch_item(_definition('later', identifier='later', script_id='demo'))]
    with _no_fd_growth(), monkeypatch.context() as patch:
        patch.setattr(history, 'record_version', record)
        if interrupted:
            with pytest.raises(KeyboardInterrupt):
                commit_asset_batch(items, owner_home=tmp_path)
            journal = json.loads((tmp_path / '.asset-batch.json').read_text())
            assert journal['committed'] is True
            before = history.pending_publication(tmp_path, 'workflow', 'flow')
            assert before['phase'] == 'committed'
            with asset_root_lock(tmp_path):
                pass
        else:
            assert commit_asset_batch(items, owner_home=tmp_path)['committed'] is True
            before = history.pending_publication(tmp_path, 'workflow', 'flow')
        assert not (tmp_path / '.asset-batch.json').exists()
        assert history.pending_publication(tmp_path, 'workflow', 'flow') == before
        assert before['snapshot']['payload']['body_markdown'] == 'B'
        assert history.latest_version(tmp_path, 'workflow', 'later')['pins']['scripts']['demo']['version'] == 1
        with asset_root_lock(tmp_path):
            pass
        assert _call(tmp_path, action='delete', workflow_id='flow')['deleted'] is True
        _retained(tmp_path, before)
        assert history.pending_publication(tmp_path, 'workflow', 'flow') is None
    assert _save(tmp_path, _definition('healthy', script_id='demo'))['version_recording']['recorded'] is True


def test_batch_first_pin_read_failure_retains_exact_body_without_later_pins(tmp_path, monkeypatch):
    _script(tmp_path)
    original, calls = history.find_version, []

    def read(home, kind, asset_id, **kwargs):
        if kind == 'script' and not calls:
            calls.append(asset_id)
            raise OSError('one first batch pin read failure')
        return original(home, kind, asset_id, **kwargs)

    with _no_fd_growth(), monkeypatch.context() as patch:
        patch.setattr(history, 'find_version', read)
        assert commit_asset_batch([_batch_item(_definition('B', script_id='demo'))], owner_home=tmp_path)['committed']
    assert calls == ['demo']
    assert not (tmp_path / '.asset-batch.json').exists()
    proof = history.pending_publication(tmp_path, 'workflow', 'flow')
    assert proof['phase'] == 'committed'
    assert proof['snapshot']['side_files']['WORKFLOW.md'] == 'B'
    assert proof['snapshot']['error_code'] and 'pins' not in proof['snapshot']
    with _no_fd_growth():
        _script(tmp_path, code='print(2)\n')
        assert _call(tmp_path, action='delete', workflow_id='flow')['deleted'] is True
        _retained(tmp_path, proof)
        assert history.latest_version(tmp_path, 'workflow', 'flow') is None


def test_recreated_revision_collision_retains_both_exact_bodies(tmp_path, monkeypatch):
    _script(tmp_path)
    original, calls = history.record_version, []

    def record(home, kind, asset_id, **kwargs):
        if kind == 'workflow' and asset_id == 'flow':
            calls.append(kwargs['storage_revision'])
            assert len(calls) <= 8
            raise ValueError('conflicting base workflow pin')
        return original(home, kind, asset_id, **kwargs)

    with _no_fd_growth(), monkeypatch.context() as patch:
        patch.setattr(history, 'record_version', record)
        assert _save(tmp_path, _definition('A', script_id='demo'))['version_recording']['recorded'] is False
        first = history.pending_publication(tmp_path, 'workflow', 'flow')
        assert _call(tmp_path, action='delete', workflow_id='flow')['deleted'] is True
        first_path = _retained(tmp_path, first)
        raw = first_path.read_bytes()
        assert _save(tmp_path, _definition('C', script_id='demo'))['version_recording']['recorded'] is False
        second = history.pending_publication(tmp_path, 'workflow', 'flow')
        assert first != second and first['snapshot']['storage_revision'] == second['snapshot']['storage_revision'] == 1
        assert _call(tmp_path, action='delete', workflow_id='flow')['deleted'] is True
        second_path = _retained(tmp_path, second)
        assert first_path != second_path and first_path.read_bytes() == raw
        assert len(_leaves(tmp_path)) == 2
        assert history.pending_publication(tmp_path, 'workflow', 'flow') is None
        assert history.latest_version(tmp_path, 'workflow', 'flow') is None
        assert WorkflowStore(tmp_path).get_workflow('flow') is None


def test_quarantine_retry_never_clears_a_newer_active_slot(tmp_path, monkeypatch):
    _script(tmp_path)
    original, calls = history.record_version, []

    def record(home, kind, asset_id, **kwargs):
        if kind == 'workflow' and asset_id == 'flow':
            calls.append(kwargs['storage_revision'])
            assert len(calls) <= 5
            raise ValueError('conflicting base workflow pin')
        return original(home, kind, asset_id, **kwargs)

    with _no_fd_growth(), monkeypatch.context() as patch:
        patch.setattr(history, 'record_version', record)
        _save(tmp_path, _definition('A', script_id='demo'))
        first = history.pending_publication(tmp_path, 'workflow', 'flow')
        _save(tmp_path, _definition('B', script_id='demo'), expected_revision=1)
        second = history.pending_publication(tmp_path, 'workflow', 'flow')
        path = _retained(tmp_path, first)
        raw = path.read_bytes()
        receipt = history.quarantine_publication(tmp_path, 'workflow', 'flow', first, error_code='retry')
        assert receipt['publication'] == first
        assert path.read_bytes() == raw
        assert history.pending_publication(tmp_path, 'workflow', 'flow') == second
        assert len(_leaves(tmp_path)) == 1
        assert _call(tmp_path, action='delete', workflow_id='flow')['deleted'] is True
        _retained(tmp_path, second)
        assert path.read_bytes() == raw


def test_quarantined_frozen_pin_is_not_recaptured_from_later_script(tmp_path, monkeypatch):
    _script(tmp_path)
    _save(tmp_path, _definition(script_id='demo'))
    original, calls = history.record_version, []

    def record(home, kind, asset_id, **kwargs):
        if kind == 'workflow' and asset_id == 'flow' and kwargs.get('storage_revision') == 2 and len(calls) < 2:
            calls.append(kwargs['storage_revision'])
            raise ValueError('conflicting base workflow pin')
        return original(home, kind, asset_id, **kwargs)

    with _no_fd_growth(), monkeypatch.context() as patch:
        patch.setattr(history, 'record_version', record)
        assert _save(tmp_path, _definition('B', script_id='demo'), expected_revision=1)['version_recording']['recorded'] is False
        proof = history.pending_publication(tmp_path, 'workflow', 'flow')
        assert proof['snapshot']['pins']['scripts']['demo']['version'] == 1
        _script(tmp_path, code='print(2)\n')
        assert _save(tmp_path, _definition('C', script_id='demo'), expected_revision=2)['version_recording']['recorded'] is True
        assert calls == [2, 2]
        path = _retained(tmp_path, proof)
        assert json.loads(path.read_text())['publication']['snapshot']['pins']['scripts']['demo']['version'] == 1
        record = history.latest_version(tmp_path, 'workflow', 'flow')
        assert record['pins']['scripts']['demo']['version'] == 2
        assert history.blob_get(tmp_path, record['side_blobs']['WORKFLOW.md']) == b'C'
        assert history.pending_publication(tmp_path, 'workflow', 'flow') is None


def test_transient_publication_io_refuses_next_overwrite_then_retries_exact_snapshot(tmp_path, monkeypatch):
    _script(tmp_path)
    _save(tmp_path, _definition(script_id='demo'))
    original, calls = history._write_bytes, []

    def write(home, parts, data):
        if 'blobs' in parts and len(calls) < 2:
            calls.append(tuple(parts))
            raise OSError('bounded blob write I/O error')
        return original(home, parts, data)

    with _no_fd_growth(), monkeypatch.context() as patch:
        patch.setattr(history, '_write_bytes', write)
        saved = _save(tmp_path, _definition('B', script_id='demo'), expected_revision=1)
        assert saved['version_recording']['recorded'] is False
        assert saved['version_recording'].get('retryable') is True
        proof = history.pending_publication(tmp_path, 'workflow', 'flow')
        assert proof['phase'] == 'committed' and proof['snapshot']['pins']['scripts']['demo']['version'] == 1
        canonical = tmp_path / 'workflows/flow/workflow.json'
        before = canonical.read_bytes()
        result = WorkflowsTool(tmp_path).execute({'action': 'save', 'workflow_id': 'flow',
            'definition': _definition('C', script_id='demo').to_dict(), 'expected_revision': 2})
        assert result.ok is False and result.error_code == 'TOOL_EXECUTION_FAILED'
        assert canonical.read_bytes() == before
        assert WorkflowStore(tmp_path).get_workflow('flow').body_markdown == 'B'
        assert history.pending_publication(tmp_path, 'workflow', 'flow') == proof
        assert len(calls) == 2 and not _leaves(tmp_path)
    with _no_fd_growth():
        _script(tmp_path, code='print(2)\n')
        assert _save(tmp_path, _definition('C', script_id='demo'), expected_revision=2)['version_recording']['recorded'] is True
        recovered = history.get_version(tmp_path, 'workflow', 'flow', 2)
        assert history.blob_get(tmp_path, recovered['side_blobs']['WORKFLOW.md']) == b'B'
        assert recovered['pins'] == proof['snapshot']['pins']
        assert history.latest_version(tmp_path, 'workflow', 'flow')['pins']['scripts']['demo']['version'] == 2
        assert history.pending_publication(tmp_path, 'workflow', 'flow') is None
