"""真实 home 类型、有限任务详情和维护预算的回归。"""
from dataclasses import replace
from types import SimpleNamespace

from agent_py_agent.agent.local_storage.owner_read_index import OwnerReadIndex
from agent_py_agent.agent.memory_store.retention_models import MemoryRetentionPolicy
from agent_py_agent.agent.observability import task_call_retention as retention
from agent_py_agent.agent.observability import task_rounds
from agent_py_agent.agent.observability.task_call_inspector import TaskCallInspector
from agent_py_agent.agent.user_space.home_layout import home_paths
from agent_py_agent.agent.user_space.storage_lanes import StorageLaneBudget, StorageLaneRequest


def _task(home):
    task = TaskCallInspector(home, owner_id='one')
    task.upsert_task_call(dict(task_id='task', started_at=1, root_run_id='root'))
    return task


def test_summary_updates_append_only_one_task(tmp_path, monkeypatch):
    task = _task(tmp_path)
    index = tmp_path / 'data/telemetry/task_calls_index.jsonl'
    before = index.read_bytes()
    monkeypatch.setattr(task, '_records', lambda: (_ for _ in ()).throw(AssertionError('full hydration')))
    task.upsert_task_call(dict(task_id='other', started_at=2))
    assert index.read_bytes().startswith(before)
    assert len(index.read_text().splitlines()) == 2


def test_task_page_uses_existing_projection_no_tool_join(tmp_path, monkeypatch):
    task = _task(tmp_path)
    for i in range(5):
        task.append_round('task', dict(round_id=f'r{i}', duration_ms=1, prompt_tokens=1), include_rounds=False)
    OwnerReadIndex(tmp_path).advance('task_rounds', task_rounds._parts('task'), validator=task_rounds._validate_frame)
    monkeypatch.setattr(OwnerReadIndex, 'advance', lambda *a, **k: (_ for _ in ()).throw(AssertionError('write')))
    monkeypatch.setattr(task.ledger, 'query_tool_calls', lambda **k: (_ for _ in ()).throw(AssertionError('tool join')))
    page = task.get_task_call_page('task', limit=2)
    assert [row['round_id'] for row in page['rows']] == ['r0', 'r1']
    page2 = TaskCallInspector(tmp_path, owner_id='one').get_task_call_page('task', limit=2, cursor=page['next_cursor'])
    assert [row['round_id'] for row in page2['rows']] == ['r2', 'r3']


def test_round_rotation_keeps_legacy_detail(tmp_path, monkeypatch):
    task = _task(tmp_path)
    task.append_round('task', dict(round_id='first', prompt_tokens=2), include_rounds=False)
    monkeypatch.setattr(task_rounds, 'TELEMETRY_VOLUME_MAX_BYTES', 1)
    task.append_round('task', dict(round_id='second', prompt_tokens=3), include_rounds=False)
    detail = task.get_task_call_detail('task')
    assert [row['round_id'] for row in detail['rounds']] == ['first', 'second']
    assert detail['token_usage']['prompt_tokens'] == 5
    assert detail['rounds_coverage']['status'] == 'complete'


def _request(home, *, budget=None, legal_hold=False, cursor=None):
    paths = replace(home_paths(home), owner_home_dir=home, owner_retention_json=home / 'retention.json')
    return StorageLaneRequest('task_call_telemetry', paths, 365 * 86400, cursor or {},
        budget or StorageLaneBudget(10, 1024 * 1024, None), legal_hold, False, None, None)


def test_lane_budget_hold_and_unknown_preserve(tmp_path, monkeypatch):
    task = _task(tmp_path)
    task.append_round('task', dict(round_id='first'), include_rounds=False)
    task.complete_task_call('task', 'succeeded', ended_at=2)
    source = tmp_path.joinpath(*task_rounds._parts('task'))
    result = retention.maintain_task_call_telemetry_lane(_request(tmp_path, budget=StorageLaneBudget(0, 0, None)))
    assert result['items'] == 0 and result['deferred_reason'] == 'budget'
    assert retention.maintain_task_call_telemetry_lane(_request(tmp_path, legal_hold=True))['deferred_reason'] == 'legal_hold'
    monkeypatch.setattr(retention, '_canonical_terminal', lambda *a: False)
    cursor = {}
    for _ in range(8):
        result = retention.maintain_task_call_telemetry_lane(_request(tmp_path, cursor=cursor))
        cursor = result['cursor']
    assert source.exists()


def test_policy_zero_and_task_hold_preserve(tmp_path, monkeypatch):
    import json
    task = _task(tmp_path)
    task.append_round('task', dict(round_id='first'), include_rounds=False)
    task.complete_task_call('task', 'succeeded', ended_at=2)
    source = tmp_path.joinpath(*task_rounds._parts('task'))
    monkeypatch.setattr(retention, '_canonical_terminal', lambda *a: True)
    for policy in [replace(MemoryRetentionPolicy(), audit_days=0),
                   replace(MemoryRetentionPolicy(), legal_hold_task_ids=('task',))]:
        (tmp_path / 'retention.json').write_text(json.dumps(policy.to_dict()))
        cursor = {}
        for _ in range(8):
            result = retention.maintain_task_call_telemetry_lane(_request(tmp_path, cursor=cursor))
            cursor = result['cursor']
        assert source.exists()


def test_model_hook_real_home_carries_request_and_stage_numbers(tmp_path):
    from agent_py_agent.agent.agent_core.model import call_runtime
    from agent_py_agent.tests.test_telemetry_ledger_and_inspector import (
        _end_model_call,
        _model_request,
    )

    request, _ = _model_request(tmp_path)
    request.pre_call_housekeeping_ms = 4.25
    request.supervision_sweep_ms = 1.5
    ledger, call_id, _ = call_runtime.start_model_call_record(request,
        context_snapshot=SimpleNamespace(current_tokens=1, to_public_dict=lambda: {}))
    _end_model_call(ledger, call_id, 'finished')
    row = TaskCallInspector(tmp_path, owner_id='one').get_task_call_detail('model-task')['rounds'][0]
    assert row['request_id'] == 'request'
    assert row['pre_call_housekeeping_ms'] == 4.25
    assert row['supervision_sweep_ms'] == 1.5
    assert isinstance(row['request_prepare_ms'], (int, float))


def test_retirement_restarts_and_reports_retired_coverage(tmp_path, monkeypatch):
    task = _task(tmp_path)
    task.append_round('task', dict(round_id='first'), include_rounds=False)
    task.complete_task_call('task', 'succeeded', ended_at=2)
    monkeypatch.setattr(retention, '_canonical_terminal', lambda *a: True)
    cursor = {}
    deleted = 0
    for _ in range(12):
        result = retention.maintain_task_call_telemetry_lane(_request(tmp_path, cursor=cursor))
        cursor = result['cursor']
        deleted += result['deleted_items']
    assert deleted == 1
    page = task.get_task_call_page('task')
    assert page['coverage']['reason'] == 'task_rounds_retired'
    assert (tmp_path / 'data/telemetry/task_calls/task.json').exists()


def test_task_capacity_retains_existing_evidence(tmp_path, monkeypatch):
    import pytest

    task = _task(tmp_path)
    task.append_round('task', dict(round_id='first'), include_rounds=False)
    monkeypatch.setattr(task_rounds, 'TELEMETRY_TOTAL_MAX_BYTES', 1)
    with pytest.raises(ValueError, match='capacity_limited'):
        task.append_round('task', dict(round_id='second'), include_rounds=False)
    assert [row['round_id'] for row in task.get_task_call_detail('task')['rounds']] == ['first']


def test_bad_task_metadata_isolated_with_partial_diagnostics(tmp_path):
    import json

    task = _task(tmp_path)
    directory = tmp_path / 'data/telemetry/task_calls'
    bad = directory / 'bad.json'
    bad.write_text('{"secret": "do-not-echo",')
    before = bad.read_bytes()
    (directory / 'array.json').write_text('[]')
    (directory / 'wrong-fields.json').write_text(json.dumps(dict(
        schema='task_call_telemetry.v1', task_id='wrong-fields', owner_id='one', started_at=1, runs=7)))
    report = task.list_task_calls_report(limit=1)
    assert [row['task_id'] for row in report['rows']] == ['task']
    assert report['coverage']['status'] == 'partial'
    assert report['coverage']['skipped_tasks'] == 3
    assert all(row['code'] == 'TASK_TELEMETRY_RECORD_UNREADABLE' for row in report['diagnostics'])
    assert all(row['effect_outcome'] == 'not_started' for row in report['diagnostics'])
    assert 'do-not-echo' not in json.dumps(report['diagnostics'])
    assert bad.read_bytes() == before
    assert [row['task_id'] for row in task.list_task_calls()] == ['task']


def test_task_diagnostics_bounded_without_losing_skipped_count(tmp_path):
    task = _task(tmp_path)
    directory = tmp_path / 'data/telemetry/task_calls'
    for i in range(20):
        (directory / f'bad-{i}.json').write_bytes(b'{')
    report = task.list_task_calls_report()
    assert report['coverage']['skipped_tasks'] == 20
    assert len(report['diagnostics']) == 16
    assert [row['task_id'] for row in report['rows']] == ['task']


def test_plugin_task_partial_envelope_real_http_roundtrip(tmp_path):
    import http.client
    import json

    from agent_py_agent.agent import plugin_host_api as api
    from agent_py_agent.agent.gateway_parts.http_service import (
        GatewayHTTPServer,
        GatewayHTTPServerParams,
    )
    from agent_py_agent.agent.gateway_parts.paths import gateway_paths_from_root
    from agent_py_agent.tests.test_plugin_host_api import _Ref

    task = TaskCallInspector(tmp_path, owner_id='local/main')
    task.append_round('healthy', dict(round_id='finished-call', status='finished', started_at=1),
                      create_if_missing=True, include_rounds=False)
    bad = tmp_path / 'data/telemetry/task_calls/bad.json'
    bad.write_bytes(b'{"secret":"do-not-echo",')
    server = GatewayHTTPServer(0, gateway_paths_from_root(tmp_path / 'gateway'),
        params=GatewayHTTPServerParams(agent=SimpleNamespace(home_paths=home_paths(tmp_path))))
    server.start()
    connection = None
    try:
        port = server.server.server_address[1]
        ref = _Ref()
        ref.owner_value = SimpleNamespace(home_dir=tmp_path, owner_id='local/main')
        env = api.issue_host_api_env(ref, 'harness')
        connection = http.client.HTTPConnection('127.0.0.1', port, timeout=5)
        connection.request('POST', '/plugin-host/query', json.dumps(dict(
            action='telemetry.tasks.list', payload=dict(limit=1))),
            {'Content-Type': 'application/json', 'X-Plugin-Host-Token': env[api.HOST_API_TOKEN_ENV]})
        response = connection.getresponse()
        envelope = json.loads(response.read())
        assert response.status == 200 and envelope['ok'] is True
        assert isinstance(envelope['data'], list)
        assert [row['task_id'] for row in envelope['data']] == ['healthy']
        assert envelope['data'][0]['status'] is None
        assert envelope['data'][0]['trigger_kind'] is None
        assert envelope['data'][0]['status_source'] == 'unconfirmed'
        assert 'message_lifecycle' in envelope['data'][0]['lifecycle_notice']['next_action']
        assert envelope['coverage']['status'] == 'partial'
        assert envelope['coverage']['skipped_tasks'] == 1
        assert envelope['diagnostics'][0]['code'] == 'TASK_TELEMETRY_RECORD_UNREADABLE'
        assert 'do-not-echo' not in json.dumps(envelope['diagnostics'])
    finally:
        if connection is not None:
            connection.close()
        server.stop()


def test_real_child_runparams_propagate_host_lineage_to_model_and_tool(tmp_path):
    from agent_py_agent.agent.agent_core.model import call_runtime
    from agent_py_agent.agent.agent_core.runtime.loop_models import RunParams
    from agent_py_agent.agent.agent_core.subagent.run_flow import (
        SubagentModelIteration,
        _subagent_model_run_params,
    )
    from agent_py_agent.agent.observability.telemetry_ledger import ToolTelemetryLedger
    from agent_py_agent.agent.subagents.context_bundle_refs import runtime_task_attributes
    from agent_py_agent.agent.subagents.models import SubAgentExecutionContext, SubAgentTask
    from agent_py_agent.agent.verification.repository import VerificationContext
    from agent_py_agent.agent.verification.runtime import _record_tool_telemetry
    from agent_py_agent.tests.test_telemetry_ledger_and_inspector import (
        _end_model_call,
        _model_request,
    )
    from agent_py_agent.tests.test_verification_runtime import _call, _success

    task = SubAgentTask(id='child-run', goal='核实回执', thought='', plan=[], parent_id='parent-run', root_id='root-run',
        attributes={'run_id': 'inherited-parent', 'parent_run_id': 'wrong-parent', 'root_run_id': 'wrong-root',
                    'conversation_thread_id': 'parent-thread'})
    attrs = runtime_task_attributes(task)
    context = SubAgentExecutionContext(run_id=task.id, generated_at=1, goal=task.goal, thought='', plan=[],
                                      parent_id=task.parent_id, root_id=task.root_id)
    params = _subagent_model_run_params(context=context, prompt='核实回执', attempt_id='exact-child-attempt',
        task_attributes=attrs, iteration=SubagentModelIteration(SimpleNamespace(injection='', history_seed=[], compact_context=None),
            [], [], None, []))
    assert isinstance(params, RunParams)
    assert (params.request_id, params.run_id, params.task_id) == ('exact-child-attempt', 'child-run', 'root-run')
    request, _ = _model_request(tmp_path)
    request.params = params
    request.agent._current_run_params = params
    ledger, call_id, _ = call_runtime.start_model_call_record(request,
        context_snapshot=SimpleNamespace(current_tokens=1, to_public_dict=lambda: {}))
    _end_model_call(ledger, call_id, 'finished')
    call = _call('read_file', {'path': 'material.txt'})
    _record_tool_telemetry(request.agent, VerificationContext('one', 'parent-thread', 'root-run'),
                          call, _success(call, '已读取', {}))
    detail = TaskCallInspector(tmp_path, owner_id='one').get_task_call_detail('root-run')
    model_row = detail['rounds'][0]
    tool_row = ToolTelemetryLedger(tmp_path, owner_id='one').get_tool_call(call.call_id)
    for row in (model_row, tool_row):
        assert (row['run_id'], row['parent_run_id'], row['root_run_id']) == ('child-run', 'parent-run', 'root-run')
    assert model_row['request_id'] == 'exact-child-attempt'
    # finished 只说明这次模型调用结束，不能签发 TaskRun closed。
    assert detail['status'] is None and detail['status_source'] == 'unconfirmed'


def test_public_unconfirmed_header_and_explicit_records_do_not_read_runtime_db(tmp_path, monkeypatch):
    import json

    from agent_py_agent.agent.runtime_db import repository

    def forbid(*args, **kwargs):
        raise AssertionError('public telemetry must not add per-row canonical DB reads')
    monkeypatch.setattr(repository, 'RuntimeRepository', forbid)
    task = TaskCallInspector(tmp_path, owner_id='one')
    task.append_round('auto', dict(round_id='round', status='finished', started_at=1),
                      create_if_missing=True, include_rounds=False)
    raw_path = tmp_path / 'data/telemetry/task_calls/auto.json'
    raw = json.loads(raw_path.read_text())
    # 模拟上线前旧遥测头：没有新可选来源字段。
    raw.pop('status_source')
    raw.pop('trigger_kind_source')
    raw_path.write_text(json.dumps(raw))
    before = raw_path.read_bytes()
    detail = task.get_task_call_detail('auto')
    assert detail['status'] is None and detail['trigger_kind'] is None
    assert detail['lifecycle_notice']['code'] == 'TASK_TELEMETRY_LIFECYCLE_UNCONFIRMED'
    assert 'message_lifecycle' in detail['lifecycle_notice']['next_action']
    assert '禁止重跑' in detail['lifecycle_notice']['next_action']
    assert raw_path.read_bytes() == before
    task.upsert_task_call(dict(task_id='explicit', started_at=2, status='running', trigger_kind='workflow'))
    report = task.list_task_calls_report(limit=1, cursor=1)
    assert report['rows'][0]['task_id'] == 'explicit'
    assert report['rows'][0]['status'] == 'running'
    assert report['rows'][0]['trigger_kind'] == 'workflow'
    assert report['rows'][0]['status_source'] == 'explicit_record'
    assert task.list_task_calls(status='running')[0]['task_id'] == 'explicit'
    task.complete_task_call('auto', 'succeeded', ended_at=3)
    assert task.get_task_call_detail('auto')['status'] == 'succeeded'
    old = json.loads(raw_path.read_text())
    old.pop('status_source')
    old.pop('trigger_kind_source')
    raw_path.write_text(json.dumps(old))
    assert task.get_task_call_detail('auto')['status'] == 'succeeded'
    assert task.get_task_call_detail('auto')['status_source'] == 'legacy_record'
    task.append_round('auto', dict(round_id='late-call', status='finished'), include_rounds=False)
    assert task.get_task_call_detail('auto')['status'] == 'succeeded'
    assert task.get_task_call_detail('auto')['status_source'] == 'legacy_record'

    # 新可选来源字段损坏仍只隔离该项，不把健康列表重新变成全页异常。
    bad = {**old, 'task_id': 'bad-source', 'status_source': []}
    (raw_path.parent / 'bad-source.json').write_text(json.dumps(bad))
    report = task.list_task_calls_report()
    assert report['coverage']['skipped_tasks'] == 1
    assert {row['task_id'] for row in report['rows']} == {'auto', 'explicit'}
