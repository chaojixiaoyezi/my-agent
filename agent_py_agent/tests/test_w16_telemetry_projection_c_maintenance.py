"""真实原C维护在保留partial时只推进工具审计投影，不授予退役权。"""
import json
from pathlib import Path

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
