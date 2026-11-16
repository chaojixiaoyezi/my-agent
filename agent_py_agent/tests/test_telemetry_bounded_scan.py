"""审计投影只读与跨实例游标的行为回归。"""
from contextlib import contextmanager

import pytest

from agent_py_agent.agent.local_storage.owner_read_index import OwnerReadIndex
from agent_py_agent.agent.observability.telemetry_ledger import PARTS, ToolTelemetryLedger


def _ledger(home, count=12):
    ledger = ToolTelemetryLedger(home, owner_id='one')
    for i in range(count):
        ledger.record_tool_call(dict(call_id=f'c{i}', owner_id='one', task_id='task', tool_name='read',
            started_at=i, ended_at=i + 1, status='error' if i == count - 1 else 'ok',
            error_code='TOOL_TIMEOUT' if i == count - 1 else ''))
    return ledger


def _snapshot(root):
    return {str(path.relative_to(root)): (path.stat().st_size, path.stat().st_mtime_ns, path.stat().st_mode)
            for path in root.rglob('*')}


def test_sparse_window_no_count_and_zero_matches_advance(tmp_path, monkeypatch):
    ledger = _ledger(tmp_path)
    view = ledger._read_index.advance('telemetry', (*PARTS, 'tool_calls.jsonl'))
    original = ledger._read_index._database
    statements = []

    @contextmanager
    def database(**kwargs):
        with original(**kwargs) as connection:
            connection.set_trace_callback(statements.append)
            yield connection

    monkeypatch.setattr(ledger._read_index, '_database', database)
    seen, after, scans = [], None, []
    while True:
        page = ledger.query_tool_calls_page(view=view, limit=1, error_code='TOOL_TIMEOUT',
            include_total=False, scan_limit=3, after_cursor=after)
        assert page['total_count'] is None
        scans.append(page['scanned'])
        seen.extend(row['call_id'] for row in page['rows'])
        after = page['next_cursor']
        if not after:
            break
    assert seen == ['c11']
    assert scans == [3, 3, 3, 3]
    assert not any('COUNT(' in sql.upper() for sql in statements)
    page = ledger.query_tool_calls_page(view=view, limit=0, include_total=False, scan_limit=3)
    assert page['scanned'] == 0


def test_readonly_missing_projection_creates_nothing(tmp_path, monkeypatch):
    ledger = _ledger(tmp_path, 1)
    before = _snapshot(tmp_path)
    monkeypatch.setattr(OwnerReadIndex, 'advance', lambda *a, **k: pytest.fail('audit advanced projection'))
    page = ledger.query_retained_tool_calls_page()
    assert page['coverage']['status'] == 'unavailable'
    assert page['coverage']['reason'] == 'index_missing'
    assert _snapshot(tmp_path) == before


def test_portable_restart_filter_and_generation_binding(tmp_path, monkeypatch):
    ledger = _ledger(tmp_path, 7)
    ledger._read_index.advance('telemetry', (*PARTS, 'tool_calls.jsonl'))
    before = _snapshot(tmp_path)
    original = OwnerReadIndex._database

    @contextmanager
    def readonly(self, **kwargs):
        assert not kwargs.get('create') and not kwargs.get('write')
        with original(self, **kwargs) as connection:
            yield connection

    monkeypatch.setattr(OwnerReadIndex, '_database', readonly)
    monkeypatch.setattr(OwnerReadIndex, 'advance', lambda *a, **k: pytest.fail('audit advanced projection'))
    page = OwnerReadIndex(tmp_path).query_readonly_page('telemetry', (*PARTS, 'tool_calls.jsonl'), limit=2, scan_limit=3)
    found = [row['call_id'] for row in page['rows']]
    token = page['next_cursor']
    with pytest.raises(ValueError, match='scope/filter/source'):
        OwnerReadIndex(tmp_path).query_readonly_page('telemetry', (*PARTS, 'tool_calls.jsonl'),
            cursor=token, scan_limit=3, filters={'status': 'error'})
    while token:
        page = OwnerReadIndex(tmp_path).query_readonly_page('telemetry', (*PARTS, 'tool_calls.jsonl'),
            limit=2, scan_limit=3, cursor=token)
        found.extend(row['call_id'] for row in page['rows'])
        token = page['next_cursor']
    assert found == [f'c{i}' for i in range(7)]
    assert _snapshot(tmp_path) == before


def test_readonly_stale_projection_has_no_repair(tmp_path, monkeypatch):
    ledger = _ledger(tmp_path, 1)
    ledger._read_index.advance('telemetry', (*PARTS, 'tool_calls.jsonl'))
    ledger.record_tool_call(dict(call_id='late', tool_name='read', started_at=2, ended_at=3))
    before = _snapshot(tmp_path)
    monkeypatch.setattr(OwnerReadIndex, 'advance', lambda *a, **k: pytest.fail('audit repaired stale projection'))
    page = OwnerReadIndex(tmp_path).query_readonly_page('telemetry', (*PARTS, 'tool_calls.jsonl'))
    assert page['coverage']['status'] == 'partial'
    assert page['coverage']['reason'] == 'index_stale'
    assert _snapshot(tmp_path) == before


def test_portable_cursor_survives_subprocess(tmp_path):
    import json
    import subprocess
    import sys

    ledger = _ledger(tmp_path, 5)
    ledger._read_index.advance('telemetry', (*PARTS, 'tool_calls.jsonl'))
    page = OwnerReadIndex(tmp_path).query_readonly_page('telemetry', (*PARTS, 'tool_calls.jsonl'), limit=2)
    program = """import json,sys
from agent_py_agent.agent.local_storage.owner_read_index import OwnerReadIndex
page=OwnerReadIndex(sys.argv[1]).query_readonly_page('telemetry',('data','telemetry','tool_calls.jsonl'),limit=2,cursor=sys.argv[2])
print(json.dumps([r['call_id'] for r in page['rows']]))
"""
    result = subprocess.run([sys.executable, '-c', program, str(tmp_path), page['next_cursor']],
        capture_output=True, text=True, check=True)
    assert json.loads(result.stdout) == ['c2', 'c3']


def test_changed_source_generation_rejects_locator(tmp_path):
    ledger = _ledger(tmp_path, 5)
    ledger._read_index.advance('telemetry', (*PARTS, 'tool_calls.jsonl'))
    page = OwnerReadIndex(tmp_path).query_readonly_page('telemetry', (*PARTS, 'tool_calls.jsonl'), limit=2)
    source = tmp_path.joinpath(*PARTS, 'tool_calls.jsonl')
    raw = source.read_bytes()
    source.unlink()
    source.write_bytes(raw)
    source.chmod(0o600)
    ledger._read_index.advance('telemetry', (*PARTS, 'tool_calls.jsonl'))
    resumed = OwnerReadIndex(tmp_path).query_readonly_page('telemetry', (*PARTS, 'tool_calls.jsonl'),
        limit=2, cursor=page['next_cursor'])
    assert resumed['coverage']['status'] == 'unavailable'
    assert resumed['coverage']['reason'] == 'stale_generation'
