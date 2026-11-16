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


@pytest.mark.parametrize('tail', [b'{"interrupted":', b'\xff\xfe', b'not-json'])
def test_append_seals_torn_tail_and_preserves_original_evidence(tmp_path, tail):
    ledger = _ledger(tmp_path, 1)
    source = tmp_path.joinpath(*PARTS, 'tool_calls.jsonl')
    original = source.read_bytes()
    with source.open('ab') as stream:
        stream.write(tail)
    ledger.record_tool_call(dict(call_id='after-recovery', owner_id='one', tool_name='read',
        started_at=3, ended_at=4))
    rows = ledger.query_tool_calls()
    assert [row['call_id'] for row in rows] == ['c0', 'after-recovery']
    assert source.read_bytes().startswith(original)
    sidecars = list(source.parent.glob('.tool_calls.jsonl.torn-*'))
    assert len(sidecars) == 1 and sidecars[0].read_bytes() == tail
    assert sidecars[0].stat().st_mode & 0o777 == 0o600
    # 重启/再次追加不重放旧残尾，也不让损坏再次粘到后续行。
    ToolTelemetryLedger(tmp_path, owner_id='one').record_tool_call(dict(
        call_id='after-restart', tool_name='read', started_at=5, ended_at=6))
    assert len(ledger.query_tool_calls()) == 3


def test_complete_json_tail_gets_newline_without_quarantine(tmp_path):
    ledger = _ledger(tmp_path, 1)
    source = tmp_path.joinpath(*PARTS, 'tool_calls.jsonl')
    source.write_bytes(source.read_bytes().rstrip(b'\n'))
    ledger.record_tool_call(dict(call_id='second', tool_name='read', started_at=3, ended_at=4))
    assert [row['call_id'] for row in ledger.query_tool_calls()] == ['c0', 'second']
    assert not list(source.parent.glob('.tool_calls.jsonl.torn-*'))


def test_quarantine_failure_keeps_torn_source_unmodified(tmp_path, monkeypatch):
    from agent_py_agent.agent.observability import telemetry_ledger as module

    ledger = _ledger(tmp_path, 1)
    source = tmp_path.joinpath(*PARTS, 'tool_calls.jsonl')
    with source.open('ab') as stream:
        stream.write(b'{"interrupted":')
    before = source.read_bytes()
    def fail(*args, **kwargs):
        raise OSError('controlled evidence persistence failure')
    monkeypatch.setattr(module, 'write_bytes_atomic_beneath', fail)
    with pytest.raises(OSError, match='evidence persistence failure'):
        ledger.record_tool_call(dict(call_id='must-not-append', tool_name='read', started_at=3, ended_at=4))
    assert source.read_bytes() == before


@pytest.mark.parametrize('link_kind', ['hardlink', 'symlink'])
def test_tail_repair_never_mutates_linked_source(tmp_path, link_kind):
    import os

    home = tmp_path / 'owner'
    directory = home.joinpath(*PARTS)
    directory.mkdir(parents=True)
    outside = tmp_path / 'independent-evidence'
    outside.write_bytes(b'{"interrupted":')
    source = directory / 'tool_calls.jsonl'
    if link_kind == 'hardlink':
        os.link(outside, source)
    else:
        source.symlink_to(outside)
    before = outside.read_bytes()
    with pytest.raises(OSError):
        ToolTelemetryLedger(home).record_tool_call(dict(call_id='forbidden', tool_name='read', started_at=1, ended_at=2))
    assert outside.read_bytes() == before
    assert not list(directory.glob('.tool_calls.jsonl.torn-*'))


@pytest.mark.parametrize('domain,table', [('telemetry', 'ori_telemetry'), ('task_rounds', 'ori_task_rounds')])
def test_missing_projection_table_rebuilds_each_source_without_stale_empty(tmp_path, domain, table):
    import json
    import sqlite3

    from agent_py_agent.agent.common.nofollow_fs import append_text_beneath
    from agent_py_agent.agent.observability.task_rounds import _frame, _validate_frame

    sources = [('data', 'first.jsonl'), ('data', 'second.jsonl')]
    validator = None if domain == 'telemetry' else _validate_frame
    if domain == 'telemetry':
        ledger = _ledger(tmp_path, 3)
        canonical = tmp_path.joinpath(*PARTS, 'tool_calls.jsonl').read_bytes()
    else:
        canonical = b''.join((json.dumps(_frame('a' * 64, i + 1, dict(round_id=f'r{i}'))) + '\n').encode()
                             for i in range(3))
    for parts in sources:
        append_text_beneath(tmp_path, parts, canonical.decode())
    index = OwnerReadIndex(tmp_path)
    old = [index.advance(domain, parts, validator=validator) for parts in sources]
    assert all(view['status'] == 'complete' for view in old)
    with sqlite3.connect(tmp_path / 'cache/owner_read_index.sqlite3') as connection:
        connection.execute(f'DROP TABLE {table}')
        assert connection.execute('SELECT COUNT(*) FROM ori_sources WHERE domain=?', (domain,)).fetchone()[0] == 2
    readonly = OwnerReadIndex(tmp_path).query_readonly_page(domain, sources[0], validator=validator)
    assert readonly['coverage']['status'] == 'unavailable'
    # 第一次只推进一条，不能继续拿旧 complete 水位宣称空集合。
    first = index.advance(domain, sources[0], validator=validator, max_rows=1)
    assert first['status'] == 'partial' and first['generation'] != old[0]['generation']
    for parts, previous in zip(sources, old):
        for _ in range(4):
            view = index.advance(domain, parts, validator=validator, max_rows=1)
            if view['status'] == 'complete':
                break
        page = index.query(view)
        assert page['coverage']['status'] == 'complete' and len(page['rows']) == 3
        assert view['generation'] != previous['generation']
        assert tmp_path.joinpath(*parts).read_bytes() == canonical
    stale = index.query(old[1])
    assert stale['coverage']['status'] == 'unavailable'
