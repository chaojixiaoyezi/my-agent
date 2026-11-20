import json
import sqlite3
from contextlib import closing, contextmanager
from dataclasses import fields
from pathlib import Path
from types import SimpleNamespace

from agent_py_agent.agent.gateway_parts import background_resource_report as report
from agent_py_agent.agent.settings.config import AgentConfig, load_config
from agent_py_agent.agent.settings.parameter_registry import parameter_registry
from agent_py_agent.agent.workflows.models import WorkflowRunRecord
from agent_py_agent.agent.workflows.reference_sources import WorkflowReferenceReader
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
