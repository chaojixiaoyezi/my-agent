import json
import os
import sqlite3
import time
from contextlib import closing, contextmanager
from dataclasses import fields
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.conversation.business_spaces import BusinessSpaceService
from agent_py_agent.agent.gateway_parts import background_resource_report as report
from agent_py_agent.agent.settings.config import AgentConfig, load_config
from agent_py_agent.agent.settings.parameter_registry import parameter_registry
from agent_py_agent.agent.workflows.business_scope import WorkflowPolicyRefusal
from agent_py_agent.agent.workflows.models import WorkflowRunRecord
from agent_py_agent.agent.workflows.reference_sources import (
    WorkflowManagementService,
    WorkflowReferenceReader,
)
from agent_py_agent.agent.workflows.run_list_index import RunListIndex
from agent_py_agent.agent.workflows.run_store import WorkflowRunStore


def test_report_rotates_settled_candidates(tmp_path, monkeypatch):
    runs = tmp_path / 'data/workflow_runs'
    runs.mkdir(parents=True)
    ids = [f'run-{n:05}' for n in range(10_000)]
    with RunListIndex(tmp_path).connection() as conn:
        conn.executemany('INSERT INTO runs VALUES(?, ?, ?, ?, ?)',
                         ((rid, 'flow', 'succeeded', 0, 0) for rid in ids))
    for run_id in ids[:6]:
        (runs / run_id).mkdir()
    scanned = []
    open_index = RunListIndex.connection

    @contextmanager
    def bounded_connection(index):
        with open_index(index) as conn:
            conn.set_progress_handler(lambda: 1, 1000)
            try:
                yield conn
            finally:
                conn.set_progress_handler(None, 0)

    monkeypatch.setattr(RunListIndex, 'connection', bounded_connection)

    def read(directory, run_ids, task_ids):
        scanned.append(directory.name)
        return 'status:succeeded', 10

    monkeypatch.setattr(report, '_read_workflow_dir_refs', read)
    for _ in range(3):
        assert not report._load_workflow_snapshot(tmp_path, max_settled_dir_scan=2).error
    assert scanned == ids[:6]
    with closing(sqlite3.connect(RunListIndex(tmp_path).path)) as conn:
        saved = conn.execute("SELECT value FROM meta WHERE key='settled_cursor'").fetchone()
    assert json.loads(saved[0]) == [0, ids[5]]


def test_reference_pages_resume_and_invalidate_on_source_change(tmp_path):
    jobs = [{'job_id': f'job-{n:03}', 'rules': []} for n in range(105)]
    spaces = {f'space-{n:03}': {} for n in range(105)}
    target = {'kind': 'workflow', 'asset_id': 'flow'}
    jobs[-1]['execution_target'] = target
    spaces['space-104']['workflows'] = ['flow']

    def read():
        return WorkflowReferenceReader(
            tmp_path, scheduler_repo=SimpleNamespace(list_jobs=lambda **kw: (jobs, [])),
            space_store={'spaces': spaces},
        )('flow')

    assert read()['coverage'] == 'unknown'
    second = read()
    assert {'job-104', 'space-104'} <= {item['ref'] for item in second['blockers']}
    assert second['coverage_generation']
    jobs[0]['execution_target'] = target
    assert 'job-000' in {item['ref'] for item in read()['blockers']}


def test_health_cursor_preserves_v4_rows_and_ties(tmp_path):
    index = RunListIndex(tmp_path)
    with index.connection() as conn:
        conn.execute('PRAGMA user_version=4')
        conn.execute('DROP TABLE IF EXISTS reference_source_cursor')
        conn.executemany('INSERT INTO runs VALUES(?, ?, ?, ?, ?)',
                         [(rid, 'flow', 'succeeded', -1, 0) for rid in ('a', 'b', 'c')])
    for rid in ('a', 'b', 'c'):
        directory = tmp_path / 'data/workflow_runs' / rid
        directory.mkdir(parents=True)
        record = WorkflowRunRecord(workflow_run_id=rid, workflow_id='flow', owner_id='local/main',
                                   trigger_kind='manual', status='succeeded', steps=(), started_at=1,
                                   ended_at=2, duration_ms=1000)
        (directory / 'run.json').write_text(json.dumps(record.to_dict()), encoding='utf-8')

    first = WorkflowRunStore(tmp_path).sample_runs_readonly(workflow_id='flow', limit=2)
    assert [item.workflow_run_id for item in first['runs']] == ['a', 'b']
    second = WorkflowRunStore(tmp_path).sample_runs_readonly(workflow_id='flow', limit=2,
                                                            after=first['next_cursor'])
    assert [item.workflow_run_id for item in second['runs']] == ['c']
    with closing(sqlite3.connect(tmp_path / 'data/workflow_run_index.sqlite3')) as conn:
        assert conn.execute('PRAGMA user_version').fetchone()[0] == 5
        assert "updated_at" not in {row[1] for row in conn.execute('PRAGMA table_info(runs)')}


def test_report_sample_size_is_loaded_from_config(tmp_path, monkeypatch):
    key = 'background_report_settled_sample_entries'
    path = tmp_path / 'agent.yaml'
    path.write_text(f'{key}: 3\n', encoding='utf-8')
    config = load_config(path)
    assert getattr(config, key) == 3
    field = next(item for item in fields(AgentConfig) if item.name == key)
    assert field.default == 256 and field.metadata['bounds'] == (1, None)
    assert field.metadata['unit'] == '条' and 'model_read_only' not in field.metadata
    spec = parameter_registry()[key]
    assert spec.tier == 'deployment' and not spec.user_editable
    catalog = json.loads((Path(__file__).resolve().parents[1] / 'config/constants_catalog.json').read_text())
    assert any(row['name'] == 'DEFAULT_BACKGROUND_REPORT_SETTLED_SAMPLE_ENTRIES' and row['value'] == 256
               for row in catalog['constants'])

    monkeypatch.setenv('MY_AGENT_CONFIG', str(path))
    calls = []
    monkeypatch.setattr(report.ProcessSessionStore, 'list_records', lambda _store: ([{}], []))
    monkeypatch.setattr(report, '_classify_execution_record', lambda *args, **kw: {
        'classification': 'unknown',
    })
    monkeypatch.setattr(report, '_load_workflow_snapshot',
                        lambda owner, **kw: (calls.append(kw) or report._WorkflowSnapshot()))
    report.freeze_settled_leftover_processes(tmp_path, store_root=tmp_path)
    assert calls[0]['max_settled_dir_scan'] == 3


class _RuntimeChurnScheduler:
    """list_jobs mirrors the repository contract: whole records ordered by next run time and ID."""

    def __init__(self, count):
        self.jobs = [{'job_id': f'job-{n:03}', 'status': 'active', 'rules': [], 'run_count': 0,
                      'next_run_at': float(n), 'last_finished_at': 0.0} for n in range(count)]

    def complete_runs(self, cycle):
        # Ordinary completions only touch runtime counters, timestamps and next-run order.
        for job in self.jobs:
            job['run_count'] += 1
            job['last_finished_at'] = 1000.0 + cycle
            job['next_run_at'] = float((hash(job['job_id']) + cycle * 7919) % 10_007)

    def list_jobs(self, *, include_deleted=False):
        selected = [dict(job) for job in self.jobs if include_deleted or job['status'] != 'deleted']
        return sorted(selected, key=lambda job: (job['next_run_at'], job['job_id'])), []


def _scheduler_read(tmp_path, scheduler):
    # A fresh reader per call exercises only the persisted cursor.
    return WorkflowReferenceReader(tmp_path, scheduler_repo=scheduler, space_store={'spaces': {}})('flow')


def test_runtime_only_scheduler_updates_keep_reference_coverage_progress(tmp_path):
    scheduler = _RuntimeChurnScheduler(250)
    target = {'kind': 'workflow', 'asset_id': 'flow'}
    scheduler.jobs[0]['execution_target'] = target
    scheduler.jobs[249]['rules'] = [{'execution_target': target}]
    results = []
    for cycle in range(3):
        results.append(_scheduler_read(tmp_path, scheduler))
        scheduler.complete_runs(cycle)
    assert [result['coverage'] for result in results] == ['unknown', 'unknown', 'complete']
    refs = {item['ref'] for item in results[-1]['blockers'] if item['kind'] == 'schedule'}
    assert refs == {'job-000', 'job-249'}
    scheduler.complete_runs(9)
    assert _scheduler_read(tmp_path, scheduler)['coverage'] == 'complete'


@pytest.mark.parametrize('change', ['new_reference', 'deleted_reference', 'unparseable_rule'])
def test_reference_bearing_scheduler_changes_invalidate_coverage(tmp_path, change):
    scheduler = _RuntimeChurnScheduler(150)
    target = {'kind': 'workflow', 'asset_id': 'flow'}
    scheduler.jobs[0]['execution_target'] = target
    assert _scheduler_read(tmp_path, scheduler)['coverage'] == 'unknown'
    complete = _scheduler_read(tmp_path, scheduler)
    assert complete['coverage'] == 'complete'
    if change == 'new_reference':
        scheduler.jobs[10]['execution_target'] = target
    elif change == 'deleted_reference':
        scheduler.jobs[0]['status'] = 'deleted'
    else:
        scheduler.jobs[20]['rules'] = None
    first = _scheduler_read(tmp_path, scheduler)
    assert first['coverage'] == 'unknown'
    if change == 'unparseable_rule':
        # An unreadable reference field fails closed instead of reusing the completed cursor.
        assert 'SCHEDULER_REFERENCE_SOURCE_INCOMPLETE' in {item['reason_code'] for item in first['blockers']}
        return
    assert first['coverage_generation']['scheduler'] != complete['coverage_generation']['scheduler']
    final = _scheduler_read(tmp_path, scheduler)
    refs = {item['ref'] for item in final['blockers'] if item['kind'] == 'schedule'}
    assert final['coverage'] == 'complete'
    assert refs == ({'job-000', 'job-010'} if change == 'new_reference' else set())


def test_business_runtime_fields_do_not_reset_but_bindings_do(tmp_path):
    spaces = {f'space-{n:03}': {'state': 'active', 'updated_at': 0} for n in range(150)}
    bindings = {}
    catalog = {'spaces': spaces, 'bindings': bindings, 'subscriptions': {}}

    def read():
        return WorkflowReferenceReader(tmp_path, scheduler_repo=SimpleNamespace(list_jobs=lambda **kw: ([], [])),
                                       space_store=catalog)('flow')

    assert read()['coverage'] == 'unknown'
    for space in spaces.values():
        space['updated_at'] += 1
    assert read()['coverage'] == 'complete'
    bindings['b-1'] = {'asset_id': 'flow', 'space_ids': ['space-001'], 'state': 'bound'}
    assert read()['coverage'] == 'unknown'
    final = read()
    assert final['coverage'] == 'complete'
    assert 'space-001' in {item['ref'] for item in final['blockers']}


def _owner_catalog(tmp_path, *, count=2):
    owner_id = 'local/main'
    spaces = {f'space-{number:03}': {
        'owner_id': owner_id, 'space_id': f'space-{number:03}', 'name': f'Space {number}',
        'purpose': '', 'revision': 0, 'state': 'active', 'mounts': [], 'default_project_ref': None,
    } for number in range(count)}
    catalog = {'schema_version': 'business-catalog.v1', 'owner_id': owner_id, 'revision': 1,
               'spaces': spaces, 'bindings': {}, 'assets': {}, 'receipts': {}}
    path = tmp_path / 'data' / 'business' / 'catalog.json'
    path.parent.mkdir(parents=True)
    return path, catalog


def _workflow_for_recycle(api, name, *, actor):
    api.execute('workflows.create', {'definition': {'descriptor': {
        'id': name, 'kind': 'workflow', 'name': name, 'access_targets': [], 'data_sources': [],
        'credential_refs': [], 'io_contract': {}}, 'steps': []},
        'scope': {'scope_mode': 'unassigned', 'space_ids': []}},
        actor=actor, idempotency_key=f'create-{name}')


def _recycle(api, name, *, actor):
    return api.execute('workflows.recycle', {'workflow_ref': name}, actor=actor,
                       expected_revision=api.store.get_workflow(name).descriptor.revision,
                       idempotency_key=f'recycle-{name}')


def test_large_owner_catalog_advances_to_late_reference(tmp_path, monkeypatch):
    path, catalog = _owner_catalog(tmp_path, count=105)
    catalog['spaces']['space-104']['mounts'] = [{
        'package_ref': 'workflow:flow', 'enabled': True, 'position': 0, 'version_ref': None,
        'availability': {'ref': 'workflow:flow', 'state': 'available',
                         'reason_code': '', 'required_capabilities': []},
    }]
    catalog['receipts'] = {f'{number:064x}': {'fingerprint': f'{number:064x}', 'result': {'summary': 'x' * 8192}}
                           for number in range(1100)}
    path.write_text(json.dumps(catalog), encoding='utf-8')
    old = time.time() - 10
    os.utime(path, (old, old))
    assert path.stat().st_size > 8 * 1024 * 1024
    reads = []
    snapshot = BusinessSpaceService._read_snapshot

    def count_snapshot(service, *, allow_missing=False, allow_legacy_references=False):
        reads.append(service.state_path)
        return snapshot(service, allow_missing=allow_missing, allow_legacy_references=allow_legacy_references)

    monkeypatch.setattr(BusinessSpaceService, '_read_snapshot', count_snapshot)
    first = WorkflowReferenceReader(tmp_path)('flow')
    second = WorkflowReferenceReader(tmp_path)('flow')
    assert first['coverage'] == 'unknown' and second['coverage'] == 'complete'
    assert first['coverage_generation'] == second['coverage_generation']
    assert {'ref': 'space-104', 'kind': 'space', 'reason_code': 'REFERENCED_BY_SPACE'} in second['blockers']
    generation, cursor = RunListIndex(tmp_path).reference_cursor('business:flow')
    assert generation == second['coverage_generation']['business']
    assert json.loads(cursor)['offset'] == 105
    actor = SimpleNamespace(owner_id='local/main')
    api = WorkflowManagementService(tmp_path)
    _workflow_for_recycle(api, 'flow', actor=actor)
    _workflow_for_recycle(api, 'unused', actor=actor)
    with pytest.raises(WorkflowPolicyRefusal) as blocked:
        _recycle(api, 'flow', actor=actor)
    assert blocked.value.code == 'WORKFLOW_CLEANUP_BLOCKED'
    assert api.reference_reader('unused')['coverage'] == 'unknown'
    assert _recycle(api, 'unused', actor=actor)['lifecycle'] == 'recycled'
    assert reads == [path]


@pytest.mark.parametrize('bindings', [[], {'invalid': None}])
def test_malformed_owner_bindings_cannot_prove_absence(tmp_path, bindings):
    path, catalog = _owner_catalog(tmp_path)
    path.write_text(json.dumps({**catalog, 'bindings': bindings}), encoding='utf-8')
    result = WorkflowReferenceReader(tmp_path)('flow')
    assert result['coverage'] == 'unknown'
    assert 'BUSINESS_REFERENCE_SOURCE_INCOMPLETE' in {item['reason_code'] for item in result['blockers']}
    actor = SimpleNamespace(owner_id='local/main')
    api = WorkflowManagementService(tmp_path)
    _workflow_for_recycle(api, 'flow', actor=actor)
    with pytest.raises(WorkflowPolicyRefusal) as refused:
        _recycle(api, 'flow', actor=actor)
    assert refused.value.code == 'REFERENCE_COVERAGE_UNKNOWN'


def test_committed_business_journal_invalidates_completed_reference_page(tmp_path):
    path, catalog = _owner_catalog(tmp_path, count=105)
    path.write_text(json.dumps(catalog), encoding='utf-8')
    assert WorkflowReferenceReader(tmp_path)('flow')['coverage'] == 'unknown'
    completed = WorkflowReferenceReader(tmp_path)('flow')
    assert completed['coverage'] == 'complete' and completed['blockers'] == []

    catalog['revision'] += 1
    catalog['receipt_rows'] = True
    catalog['spaces']['space-104']['mounts'] = [{
        'package_ref': 'workflow:flow', 'enabled': True, 'position': 0, 'version_ref': None,
        'availability': {'ref': 'workflow:flow', 'state': 'available',
                         'reason_code': '', 'required_capabilities': []},
    }]
    journal = path.with_name('.catalog-transaction.json')
    journal.write_text(json.dumps({'schema': 'business-catalog-transaction.v1', 'owner_id': 'local/main',
        'state': catalog, 'key': 'a' * 64, 'receipt': {'fingerprint': 'b' * 64, 'result': {}}}), encoding='utf-8')
    expected_catalog = path.read_bytes()
    pending = WorkflowReferenceReader(tmp_path)('flow')
    assert pending['coverage'] == 'unknown'
    assert pending['coverage_generation']['business'] != completed['coverage_generation']['business']
    result = WorkflowReferenceReader(tmp_path)('flow')
    assert result['coverage'] == 'complete'
    assert [row['ref'] for row in result['blockers']] == ['space-104']
    assert path.read_bytes() == expected_catalog and journal.is_file()


def test_gateway_settled_sweeps_forward_the_loaded_config(tmp_path, monkeypatch):
    from dataclasses import replace

    from agent_py_agent.cli import gateway_process

    key = 'background_report_settled_sample_entries'
    monkeypatch.delenv('MY_AGENT_CONFIG', raising=False)
    calls = []
    monkeypatch.setattr(report.ProcessSessionStore, 'list_records', lambda _store: ([{}], []))
    monkeypatch.setattr(report, '_classify_execution_record', lambda *args, **kw: {'classification': 'unknown'})
    monkeypatch.setattr(report, '_load_workflow_snapshot',
                        lambda owner, **kw: (calls.append(kw['max_settled_dir_scan']) or report._WorkflowSnapshot()))
    agent = SimpleNamespace(config=replace(AgentConfig(), **{key: 16}), root=str(tmp_path),
                            home_paths=SimpleNamespace(owner_home_dir=str(tmp_path), root=None))
    Path(gateway_process._background_store_root(agent)).mkdir(parents=True, exist_ok=True)
    gateway_process._stop_settled_background_processes(agent)
    assert gateway_process.run_cold_start_settled_sweep_step(agent, {}, ttl_seconds=300, slice_ms=0)
    assert calls == [16, 16]
