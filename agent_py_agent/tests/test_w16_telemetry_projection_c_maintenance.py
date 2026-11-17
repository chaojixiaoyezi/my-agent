"""真实原C维护在保留partial时只推进工具审计投影，不授予退役权。"""
import json
from pathlib import Path

import pytest

from agent_py_agent.agent.local_storage.owner_read_index import OwnerReadIndex
from agent_py_agent.agent.observability.telemetry_ledger import (
    PARTS,
    ToolCallRecord,
    ToolTelemetryLedger,
)
from agent_py_agent.tests.test_gateway_background_curator_transactions import (
    curator_case,
    run_original,
)


def test_original_c_maintenance_indexes_healthy_calls_past_bad_row_while_retention_partial(tmp_path, monkeypatch):
    with curator_case(tmp_path, monkeypatch, source_kind='maintenance') as (agent, _, _, gate, claim, _):
        agent.config.retention_scan_slice_entries = 4
        agent.config.retention_scan_slice_ms = 100
        home = agent.home_paths.owner_home_dir
        source = Path(home).joinpath(*PARTS, 'tool_calls.jsonl')
        source.parent.mkdir(parents=True, exist_ok=True)
        records = [ToolCallRecord(call_id=str(i), tool_name='read_file', owner_id='local/main', task_id='audit-task').to_dict()
                   for i in range(3)]
        raw = (json.dumps(records[0]) + '\n{broken\n' + json.dumps(records[1]) + '\n' + json.dumps(records[2]) + '\n').encode()
        source.write_bytes(raw)
        index = OwnerReadIndex(home)
        before = ToolTelemetryLedger(home, owner_id='local/main').query_retained_tool_calls_page(task_id='audit-task')
        assert before['coverage']['reason'] == 'index_missing'
        from agent_py_agent.agent.user_space.home_index_rebuild import maintain_home_indexes_lane
        from agent_py_agent.agent.user_space.home_indexes import (
            TaskIndexRef,
            latest_task_refs,
            register_task_ref,
        )
        from agent_py_agent.agent.user_space.storage_lanes import resolve_storage_lane
        assert resolve_storage_lane('global_indexes') is maintain_home_indexes_lane
        canonical = Path(home) / 'runs' / 'day' / 'canonical'
        (canonical / 'work').mkdir(parents=True)
        (canonical / 'work/state.json').write_text(json.dumps(dict(task_id='canonical', primary_run_id='canonical-run', status='DONE')))
        register_task_ref(agent.home_paths, TaskIndexRef(owner_id='local/main', task_id='canonical',
            task_path=canonical, status='active', title='canonical'))
        response = run_original(agent, gate, claim)
        assert response['ok'] is True
        marker = json.loads((agent.home_paths.owner_data_dir / 'maintenance.json').read_text())
        assert marker['retention_scan_partial'] is True
        assert {'task_call_telemetry', 'global_indexes'} <= set(marker['storage_maintenance']['coordinator_lanes']['ran'])
        assert 'global_indexes' not in marker['storage_maintenance']['coordinator_lanes']['fallback_kinds']
        assert latest_task_refs(agent.home_paths, owner_id='local/main')[0]['status'] == 'done'
        assert marker['storage_maintenance']['deletion_enabled'] is False
        assert source.read_bytes() == raw
        assert (agent.home_paths.owner_cache_dir / 'old.tmp').exists()
        page = ToolTelemetryLedger(home, owner_id='local/main').query_retained_tool_calls_page(task_id='audit-task')
        assert {row['call_id'] for row in page['rows']} == {'0', '1', '2'}
        assert page['coverage']['skipped_records'] == 1
        assert page['coverage']['record_errors'][0]['code'] == 'invalid_json'
        # 跨实例只读仍披露已跳过坏行，不能靠进程缓存隐藏coverage。
        view = index.capture_view('telemetry', (*PARTS, 'tool_calls.jsonl'))
        assert view['skipped_records'] == 1
        assert view['status'] == 'complete'

        # 第二次真实追加后重新冻结原维护来源、经同一C admission/claim处理下一tick。
        from agent_py_agent.tests import test_gateway_background_curator_transactions as original
        ToolTelemetryLedger(home, owner_id='local/main').record_tool_call(
            ToolCallRecord(call_id='appended', tool_name='read_file', owner_id='local/main', task_id='audit-task'))
        gate.release_settled()
        ref = original.freeze_source(agent.home_paths.root, original.OwnerIdentity.local_main(),
                                     agent.home_paths.owner_data_dir / 'maintenance.json', 'maintenance')
        path, _ = original.enqueue_source(gate.paths, agent.home_paths.root, ref)
        request = original.read_json_file(path)
        decision = gate.admit(path, (original.OwnerIdentity.local_main(), original.session_scope_for_request(request)),
                              original.AdmissionLimits(8, 8))
        assert decision['status'] == 'admitted'
        next_claim = original.claim_request(gate.paths, path,
            coordinator_snapshot=original.freeze_coordinator_claim(gate, decision))
        assert next_claim is not None
        assert run_original(agent, gate, next_claim)['ok'] is True
        newer = ToolTelemetryLedger(home, owner_id='local/main').query_retained_tool_calls_page(task_id='audit-task')
        assert {row['call_id'] for row in newer['rows']} == {'0', '1', '2', 'appended'}
        assert newer['coverage']['generation'] == view['generation']


def test_projection_does_not_read_corrupt_retention_or_delete_held_ledger(tmp_path):
    from types import SimpleNamespace

    from agent_py_agent.agent.observability.telemetry_ledger import (
        maintain_tool_call_projection_lane,
    )
    from agent_py_agent.agent.user_space.storage_lanes import (
        StorageLaneBudget,
        StorageLaneRequest,
        validate_lane_receipt,
    )
    owner = tmp_path / 'owner'
    source = owner.joinpath(*PARTS, 'tool_calls.jsonl')
    source.parent.mkdir(parents=True)
    source.write_text(''.join(json.dumps(ToolCallRecord(call_id=str(i), tool_name='read_file').to_dict()) + '\n'
                              for i in range(7)))
    policy = owner / 'retention.json'
    policy.write_text('{corrupt-policy')
    original = source.read_bytes()
    cursor = {}
    for _ in range(32):
        request = StorageLaneRequest(kind='task_call_telemetry', home=SimpleNamespace(owner_home_dir=owner),
            now=200_000, cursor=cursor, budget=StorageLaneBudget(2, 4096, None),
            legal_hold=True, coordinator=True, pressure=None, commit=lambda op: op())
        receipt = validate_lane_receipt(maintain_tool_call_projection_lane(request))
        assert receipt['items'] <= 2 and receipt['bytes'] <= 4096
        assert not receipt.get('deleted_items', 0)
        cursor = receipt['cursor']
        if receipt['complete']:
            break
    else:
        raise AssertionError('bounded projection must converge')
    assert source.read_bytes() == original
    assert policy.read_text() == '{corrupt-policy'
    view = OwnerReadIndex(owner).capture_view('telemetry', (*PARTS, 'tool_calls.jsonl'))
    assert view['status'] == 'complete'


def test_invalid_rows_consume_budget_and_diagnostics_survive_restart(tmp_path):
    owner = tmp_path / 'owner'
    source = owner.joinpath(*PARTS, 'tool_calls.jsonl')
    source.parent.mkdir(parents=True)
    source.write_text('{bad\n' * 9 + json.dumps(ToolCallRecord(call_id='healthy', tool_name='read_file').to_dict()) + '\n')
    for _ in range(5):
        index = OwnerReadIndex(owner)
        view = index.advance('telemetry', (*PARTS, 'tool_calls.jsonl'), max_rows=2, max_bytes=4096,
                             max_ms=1000, skip_invalid_records=True)
        assert index._last_advance_work['items'] <= 2
    assert view['status'] == 'complete'
    assert view['skipped_records'] == 9
    page = ToolTelemetryLedger(owner).query_retained_tool_calls_page()
    assert [row['call_id'] for row in page['rows']] == ['healthy']
    assert page['coverage']['skipped_records'] == 9
    unchanged = OwnerReadIndex(owner)
    same = unchanged.advance('telemetry', (*PARTS, 'tool_calls.jsonl'), max_rows=2, max_bytes=4096,
                             max_ms=1000, skip_invalid_records=True)
    assert same['generation'] == view['generation']
    assert unchanged._last_advance_work == {'items': 0, 'bytes': 0}


def test_partial_retention_pending_resumes_completed_projection_on_next_real_tick(tmp_path):
    from dataclasses import replace
    from types import SimpleNamespace

    from agent_py_agent.agent.user_space.home_layout import ensure_my_agent_home
    from agent_py_agent.agent.user_space.home_retention import maintenance_fence
    from agent_py_agent.agent.user_space.owner_maintenance import (
        run_owner_storage_maintenance_slice,
    )
    home = replace(ensure_my_agent_home(tmp_path / 'home'), owner_id='local/main')
    source = home.owner_home_dir.joinpath(*PARTS, 'tool_calls.jsonl')
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_text(json.dumps(ToolCallRecord(call_id='new', tool_name='read_file').to_dict()) + '\n')
    previous = {'storage_maintenance': dict(continuation_pending=True, pending_kinds=['global_indexes'],
        coordinator_lanes={'pending_kinds': ['global_indexes']},
        cursors={'task_call_telemetry': {'complete': True}})}
    maintenance_fence(previous)
    retention = SimpleNamespace(applied=False, legal_hold=False, load_errors=[], scan_partial=True)
    config = SimpleNamespace(retention_scan_slice_entries=4, retention_scan_slice_bytes=8192, retention_scan_slice_ms=100)
    state = run_owner_storage_maintenance_slice(home, previous, retention, now=200_000,
        coordinator=True, commit_transition=lambda op: op(), config=config)
    assert 'task_call_telemetry' in state['coordinator_lanes']['ran']
    page = ToolTelemetryLedger(home.owner_home_dir).query_retained_tool_calls_page()
    assert [row['call_id'] for row in page['rows']] == ['new']
    assert state['deletion_enabled'] is False


def _projection_maintenance(tmp_path, monkeypatch, *, intercept=None):
    from dataclasses import replace
    from types import SimpleNamespace

    from agent_py_agent.agent.user_space import storage_lanes
    from agent_py_agent.agent.user_space.home_layout import ensure_my_agent_home
    from agent_py_agent.agent.user_space.owner_maintenance import (
        run_owner_storage_maintenance_slice,
    )

    home = replace(ensure_my_agent_home(tmp_path / 'home'), owner_id='local/main')
    original = storage_lanes.resolve_storage_lane
    def resolve(kind, *, projection_only=False):
        if kind == 'global_indexes':
            return lambda request: dict(cursor=request.cursor, complete=False, items=0, bytes=0)
        lane = original(kind, projection_only=projection_only)
        return intercept(lane) if intercept is not None and kind == 'task_call_telemetry' else lane
    monkeypatch.setattr(storage_lanes, 'resolve_storage_lane', resolve)
    config = SimpleNamespace(retention_scan_slice_entries=128, retention_scan_slice_bytes=8192,
                             retention_scan_slice_ms=1000)
    retention = SimpleNamespace(applied=False, legal_hold=False, load_errors=[], scan_partial=True)
    def tick(state=None):
        return run_owner_storage_maintenance_slice(home, {'storage_maintenance': state} if state else {},
            retention, now=200_000, coordinator=True, commit_transition=lambda op: op(), config=config)
    return home, tick


def _finish_projection(tick, state=None):
    for _ in range(32):
        state = tick(state)
        assert not state['errors']
        if state['cursors'].get('task_call_telemetry', {}).get('complete'):
            return state
    raise AssertionError('原128条/8192字节投影预算未完成')


def _retained_call_ids(ledger):
    cursor, found = '', set()
    for _ in range(32):
        page = ledger.query_retained_tool_calls_page(cursor=cursor)
        assert page['coverage']['status'] == 'complete'
        found.update(row['call_id'] for row in page['rows'])
        cursor = page['next_cursor']
        if cursor is None:
            return found
    raise AssertionError('规范遥测卷分页未结束')


def test_projection_generation_only_resumes_changed_active_or_sealed_sources(tmp_path, monkeypatch):
    home, tick = _projection_maintenance(tmp_path, monkeypatch)
    ledger = ToolTelemetryLedger(home.owner_home_dir)
    ledger.record_tool_call(ToolCallRecord(call_id='first', tool_name='read_file'))
    state = _finish_projection(tick)
    proof = json.loads(json.dumps(state['projection_source_revisions']))
    unchanged = tick(state)
    assert 'task_call_telemetry' not in unchanged['coordinator_lanes']['ran']
    assert unchanged['projection_source_revisions'] == proof
    assert unchanged['pending_kinds'] == ['global_indexes']
    ledger.record_tool_call(ToolCallRecord(call_id='appended', tool_name='read_file'))
    appended = tick(unchanged)
    assert 'task_call_telemetry' in appended['coordinator_lanes']['ran']
    assert _retained_call_ids(ledger) == {'first', 'appended'}
    state = _finish_projection(tick, appended)
    sealed = home.owner_home_dir.joinpath(*PARTS, 'tool_calls.0000000000000000001.jsonl')
    sealed.write_text(json.dumps(ToolCallRecord(call_id='sealed', tool_name='read_file').to_dict()) + '\n')
    changed = tick(state)
    assert 'task_call_telemetry' in changed['coordinator_lanes']['ran']
    state = _finish_projection(tick, changed)
    assert _retained_call_ids(ledger) == {'first', 'appended', 'sealed'}
    assert state['deletion_enabled'] is False and sealed.exists()
    assert 'task_call_telemetry' not in tick(state)['coordinator_lanes']['ran']


def test_projection_completion_does_not_save_a_generation_changed_inside_lane(tmp_path, monkeypatch):
    appended = []
    def intercept(lane):
        def advance(request):
            receipt = lane(request)
            if receipt['complete'] and not appended:
                request.run_write(lambda: ToolTelemetryLedger(request.home.owner_home_dir).record_tool_call(
                    ToolCallRecord(call_id='racing-append', tool_name='read_file')))
                appended.append(True)
            return receipt
        advance.maintenance_source_revision = lane.maintenance_source_revision
        return advance
    home, tick = _projection_maintenance(tmp_path, monkeypatch, intercept=intercept)
    ToolTelemetryLedger(home.owner_home_dir).record_tool_call(ToolCallRecord(call_id='original', tool_name='read_file'))
    state = None
    for _ in range(32):
        state = tick(state)
        if appended:
            break
    assert appended and 'task_call_telemetry' in state['pending_kinds']
    assert 'task_call_telemetry' not in state['projection_source_revisions']
    final = _finish_projection(tick, state)
    assert 'task_call_telemetry' in final['projection_source_revisions']
    assert {row['call_id'] for row in ToolTelemetryLedger(home.owner_home_dir).query_retained_tool_calls_page()['rows']} == {
        'original', 'racing-append'}


@pytest.mark.parametrize("portable", [False, True])
def test_projection_generation_probe_is_readonly_and_does_not_create_missing_sources(tmp_path, monkeypatch, portable):
    from agent_py_agent.agent.observability.telemetry_ledger import (
        maintain_tool_call_projection_lane,
    )
    from agent_py_agent.agent.user_space.home_layout import ensure_my_agent_home

    home = ensure_my_agent_home(tmp_path / 'home')
    if portable:
        from agent_py_agent.agent.common import nofollow_fs
        monkeypatch.setattr(nofollow_fs, '_supports_dir_fd', lambda: False)
    monkeypatch.setattr(OwnerReadIndex, '__init__', lambda *args, **kwargs: pytest.fail('probe cannot open or create index'))
    probe = maintain_tool_call_projection_lane.maintenance_source_revision
    assert probe(home) == ['missing']
    assert not home.owner_home_dir.joinpath(*PARTS).exists()
    source = home.owner_home_dir.joinpath(*PARTS, 'tool_calls.jsonl')
    source.parent.mkdir(parents=True)
    source.write_text('immutable fixture')
    before = source.read_bytes()
    stamp = probe(home)
    assert probe(home) == stamp and source.read_bytes() == before
    assert not (home.owner_cache_dir / 'owner_read_index.sqlite3').exists()


def test_projection_probe_failure_stays_pending_without_following_a_symlink(tmp_path, monkeypatch):
    home, tick = _projection_maintenance(tmp_path, monkeypatch)
    outside = tmp_path / 'outside'
    outside.write_text('private outside contents')
    source = home.owner_home_dir.joinpath(*PARTS, 'tool_calls.jsonl')
    source.parent.mkdir(parents=True)
    source.symlink_to(outside)
    state = tick()
    assert state['continuation_pending'] and 'task_call_telemetry' in state['pending_kinds']
    assert 'task_call_telemetry' not in state['projection_source_revisions']
    error = next(row for row in state['errors'] if row['kind'] == 'task_call_telemetry')
    assert 'TELEMETRY_PROJECTION_SOURCE_UNSAFE' in error['error_message']
    assert outside.read_text() == 'private outside contents'
    assert not (home.owner_cache_dir / 'owner_read_index.sqlite3').exists()


def test_projection_directory_continuation_restarts_only_after_membership_change(tmp_path, monkeypatch):
    from agent_py_agent.agent.observability.telemetry_ledger import (
        maintain_tool_call_projection_lane,
    )
    from agent_py_agent.agent.user_space.storage_lanes import StorageLaneBudget, StorageLaneRequest

    home, _tick = _projection_maintenance(tmp_path, monkeypatch)
    owner = home.owner_home_dir
    ToolTelemetryLedger(owner).record_tool_call(ToolCallRecord(call_id='active', tool_name='read_file'))
    def advance(cursor):
        return maintain_tool_call_projection_lane(StorageLaneRequest('task_call_telemetry', home, 200_000,
            cursor, StorageLaneBudget(128, 8192, None), False, True, None, lambda op: op()))
    first = advance({})
    assert not first['complete'] and first['cursor']['scan']
    unchanged = advance(first['cursor'])
    assert unchanged['cursor']['source_membership_revision'] == first['cursor']['source_membership_revision']
    sealed = owner.joinpath(*PARTS, 'tool_calls.0000000000000000002.jsonl')
    sealed.write_text(json.dumps(ToolCallRecord(call_id='late-sealed', tool_name='read_file').to_dict()) + '\n')
    newer = advance(unchanged['cursor'])
    assert newer['cursor']['source_membership_revision'] != first['cursor']['source_membership_revision']
    for _ in range(32):
        if newer['complete']:
            break
        newer = advance(newer['cursor'])
    assert newer['complete']
    assert _retained_call_ids(ToolTelemetryLedger(owner)) == {'active', 'late-sealed'}
