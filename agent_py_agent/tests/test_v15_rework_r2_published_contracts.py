"""发布身份的真实存储/工具/执行往返；外部模型与IM不属于本切片。"""
import json
import os
from dataclasses import replace
from pathlib import Path

import pytest

from agent_py_agent.agent.assets.datasource_store import DatasourceStore
from agent_py_agent.agent.assets.descriptor import AccessTarget, AssetDescriptor, DataSourceSpec
from agent_py_agent.agent.assets.revision_history import (
    get_version,
    latest_version,
    record_version,
)
from agent_py_agent.agent.assets.script_store import ScriptStore
from agent_py_agent.agent.assets.workflow_store import (
    WorkflowDefinition,
    WorkflowStepSpec,
    WorkflowStore,
)
from agent_py_agent.agent.tooling.scripts_tool import ScriptsTool
from agent_py_agent.agent.workflows import datasource_runtime as runtime
from agent_py_agent.agent.workflows.engine import WorkflowEngine
from agent_py_agent.agent.workflows.pinned_definitions import (
    historical_pin_available,
    resolve_definition,
)
from agent_py_agent.agent.workflows.script_runner import ScriptRunner
from agent_py_agent.tests.test_v15_w4_tool_surface import call
from agent_py_agent.tests.test_v15_w6_harness_guardrails import CountingRunner, result

CODE = 'import json\nprint(json.dumps({"value":7}))\n'


def published_script(home):
    descriptor = AssetDescriptor('probe', 'script', '固定查询', io_contract={'outputs': ['value']},
        access_targets=(AccessTarget('local', 'file', 'input.json', extra={'read_only': True}),))
    store = ScriptStore(home)
    row = store.save_script(descriptor, CODE)
    return store, descriptor, row, latest_version(home, 'script', 'probe')


def frozen_pin(row, published, *, version=True, reference=True):
    pin = {'script_id': 'probe', 'revision': row['revision'], 'code_sha256': published['code_sha256'],
           'source_scope': row['source_scope']}
    if version:
        pin['version'] = published['version']
    if reference:
        pin['execution_definition_ref'] = published['execution_definition_ref']
    return pin


@pytest.mark.parametrize('blocking', [False, True])
@pytest.mark.parametrize('reference', [False, True])
def test_normal_summary_fold_does_not_invalidate_execution(tmp_path, blocking, reference):
    store, _, row, published = published_script(tmp_path)
    pin = frozen_pin(row, published, version=False, reference=reference)
    runner = ScriptRunner(tmp_path)
    first = runtime.run_script_with_datasources(tmp_path, 'probe', runner=runner, expected_script_pin=pin)
    assert first.ok and first.output_data == {'value': 7}, first
    if blocking:
        store.record_run_summary('probe', 'succeeded', 1, blocking=True, run_id='explicit-fold')
    else:
        store.fold_run_summaries('probe')
    folded = store.get_script('probe')
    assert folded['revision'] > row['revision']
    assert folded['execution_definition_ref'] == row['execution_definition_ref']
    assert latest_version(tmp_path, 'script', 'probe')['version'] == published['version']
    second = runtime.run_script_with_datasources(tmp_path, 'probe', runner=runner, expected_script_pin=pin)
    assert second.ok and second.output_data == {'value': 7}, second
    assert second.source_check['pin_resolution'] == {'mode': 'current'}


def test_public_script_version_keeps_same_code_historical_contract(tmp_path):
    store, descriptor, _, first = published_script(tmp_path)
    current = store.save_script(replace(descriptor, io_contract={'outputs': ['later']}), CODE)
    second = latest_version(tmp_path, 'script', 'probe')
    assert second['version'] > first['version'] and second['code_sha256'] == first['code_sha256']
    assert second['execution_definition_ref'] != first['execution_definition_ref']
    output = call(ScriptsTool(tmp_path, runner=ScriptRunner(tmp_path)),
                  {'action': 'run', 'script_id': 'probe', 'version': first['version']})
    assert output['ok'] and output['output_data'] == {'value': 7}, output
    assert output['source_check']['pin_resolution']['version'] == first['version']
    assert store.get_script('probe')['io_contract'] == current['io_contract']


def test_public_same_code_history_does_not_gain_current_retry_permission(tmp_path, monkeypatch):
    targets = (AccessTarget('api', 'http', 'https://example.invalid', method='POST'),)
    old = AssetDescriptor('probe', 'script', '固定副作用', access_targets=targets,
        metadata={'retry_policy': {'idempotent': False}})
    store = ScriptStore(tmp_path)
    store.save_script(old, CODE)
    first = latest_version(tmp_path, 'script', 'probe')
    store.save_script(replace(old, metadata={'retry_policy': {'idempotent': True}}), CODE)
    second = latest_version(tmp_path, 'script', 'probe')
    assert first['code_sha256'] == second['code_sha256']
    assert first['execution_definition_ref'] == second['execution_definition_ref']
    runner = CountingRunner([result(error_code='ECONNRESET')])
    monkeypatch.setattr(runtime.time, 'sleep', lambda delay: None)
    outcome = ScriptsTool(tmp_path, runner=runner).execute(
        {'action': 'run', 'script_id': 'probe', 'version': first['version']})
    assert not outcome.ok
    output = json.loads(outcome.output)
    assert not output['ok'] and output['error_code'] == 'ECONNRESET', output
    assert len(runner.calls) == 1


@pytest.mark.parametrize('selection,attempts', [('current', 3), ('version', 3), ('old_ref', 1), ('fresh_ref', 3)])
def test_summary_fold_only_withholds_unproven_legacy_retry_permission(tmp_path, monkeypatch, selection, attempts):
    descriptor = AssetDescriptor('probe', 'script', '固定副作用',
        access_targets=(AccessTarget('api', 'http', 'https://example.invalid', method='POST'),),
        metadata={'retry_policy': {'idempotent': True}})
    store = ScriptStore(tmp_path)
    row = store.save_script(descriptor, CODE)
    published = latest_version(tmp_path, 'script', 'probe')
    store.record_run_summary('probe', 'succeeded', 1, blocking=True, run_id='fold')
    current = store.get_script('probe')
    assert current['revision'] > row['revision']
    pin = None if selection == 'current' else frozen_pin(
        current if selection == 'fresh_ref' else row, published, version=selection == 'version')
    runner = CountingRunner([result(error_code='ECONNRESET')])
    monkeypatch.setattr(runtime.time, 'sleep', lambda delay: None)
    value = runtime.run_script_with_datasources(tmp_path, 'probe', runner=runner, expected_script_pin=pin)
    assert not value.ok and value.error_code == 'ECONNRESET'
    assert len(runner.calls) == attempts
    assert store.get_script('probe')['metadata']['retry_policy']['idempotent'] is True


@pytest.mark.parametrize('old_idempotent,current_idempotent', [(False, True), (True, False)])
def test_explicit_same_code_version_intersects_both_retry_permissions(tmp_path, monkeypatch,
        old_idempotent, current_idempotent):
    old = AssetDescriptor('probe', 'script', '固定副作用',
        access_targets=(AccessTarget('api', 'http', 'https://example.invalid', method='POST'),),
        metadata={'retry_policy': {'idempotent': old_idempotent}})
    store = ScriptStore(tmp_path)
    row = store.save_script(old, CODE)
    published = latest_version(tmp_path, 'script', 'probe')
    store.save_script(replace(old, metadata={'retry_policy': {'idempotent': current_idempotent}}), CODE)
    runner = CountingRunner([result(error_code='ECONNRESET')])
    monkeypatch.setattr(runtime.time, 'sleep', lambda delay: None)
    value = runtime.run_script_with_datasources(tmp_path, 'probe', runner=runner,
        expected_script_pin=frozen_pin(row, published))
    assert not value.ok and value.error_code == 'ECONNRESET' and len(runner.calls) == 1
    assert value.source_check['pin_resolution']['version'] == published['version']


def test_public_same_code_history_keeps_query_semantics_under_current_permissions(tmp_path):
    DatasourceStore(tmp_path).save_datasource({'datasource_id': 'events', 'kind': 'custom', 'title': '事件'})
    old = AssetDescriptor('probe', 'script', '固定查询', io_contract={'outputs': ['window', 'access']},
        access_targets=(AccessTarget('local', 'file', 'input.json', extra={'read_only': True}),),
        data_sources=(DataSourceSpec('events', 'custom', query_scope={'window': 'old', 'access': 'read'}),))
    code = '''import json,os
from agent_py_agent.agent.workflows.datasource_runtime import current_datasource_bindings
print(json.dumps({"window":json.loads(os.environ["MY_AGENT_DATA_SOURCES_JSON"])[0]["query_scope"]["window"],
                  "access":current_datasource_bindings()["events"]["access"]}))
'''
    store = ScriptStore(tmp_path)
    store.save_script(old, code)
    first = latest_version(tmp_path, 'script', 'probe')
    store.save_script(replace(old, data_sources=(DataSourceSpec('events', 'custom',
        query_scope={'window': 'new', 'access': 'read_write'}),)), code)
    output = call(ScriptsTool(tmp_path, runner=ScriptRunner(tmp_path)),
                  {'action': 'run', 'script_id': 'probe', 'version': first['version']})
    assert output['ok'] and output['output_data'] == {'window': 'old', 'access': 'read'}, output
    assert output['source_check']['pin_resolution']['version'] == first['version']
    assert store.get_script('probe')['data_sources'][0]['query_scope'] == {'window': 'new', 'access': 'read_write'}


def test_explicit_version_without_ref_never_uses_new_same_sha_contract(tmp_path):
    store, descriptor, row, old = published_script(tmp_path)
    store.save_script(replace(descriptor, io_contract={'outputs': ['later']}), CODE)
    value = runtime.run_script_with_datasources(tmp_path, 'probe', runner=ScriptRunner(tmp_path),
        expected_script_pin=frozen_pin(row, old, reference=False))
    assert value.ok and value.output_data == {'value': 7}, value
    assert value.source_check['pin_resolution']['version'] == old['version']


def test_corrupt_script_blob_rejects_workflow_before_run_creation(tmp_path):
    _, _, row, script = published_script(tmp_path)
    WorkflowStore(tmp_path).save_workflow(WorkflowDefinition(AssetDescriptor('frozen', 'workflow', '冻结流程'), (
        WorkflowStepSpec('one', '第一步', 'deterministic_script', script_id='probe'),)))
    workflow = latest_version(tmp_path, 'workflow', 'frozen')
    sha = script['code_sha256']
    (tmp_path / 'data/asset_evolution/blobs' / sha[:2] / sha).write_bytes(b'corrupt')
    assert historical_pin_available(tmp_path, frozen_pin(row, script)) is False
    output = WorkflowEngine(tmp_path).run_workflow('frozen', definition_version=workflow['version'])
    assert output['error_code'] == 'ASSET_HISTORY_BLOB_MISSING' and output['effect_outcome'] == 'not_started'
    assert not list((tmp_path / 'data/workflow_runs').glob('*/run.json'))


def test_published_workflow_selects_script_version_not_latest_matching_sha(tmp_path):
    scripts, descriptor, _, old_script = published_script(tmp_path)
    workflows = WorkflowStore(tmp_path)
    workflows.save_workflow(WorkflowDefinition(AssetDescriptor('frozen', 'workflow', '冻结流程'), (
        WorkflowStepSpec('one', '第一步', 'deterministic_script', script_id='probe', on_error='fail'),
        WorkflowStepSpec('two', '第二步', 'deterministic_script', script_id='probe', on_error='fail'))))
    workflow_record = latest_version(tmp_path, 'workflow', 'frozen')
    assert workflow_record['pins']['scripts']['probe']['version'] == old_script['version']
    scripts.save_script(replace(descriptor, io_contract={'outputs': ['later']}), CODE)
    _, frozen = resolve_definition(tmp_path, 'frozen', workflow_record['version'])
    assert {pin['execution_definition_ref'] for pin in frozen.values()} == {old_script['execution_definition_ref']}
    assert {pin['version'] for pin in frozen.values()} == {old_script['version']}
    engine = WorkflowEngine(tmp_path)
    run = engine.run_workflow('frozen', definition_version=workflow_record['version'])
    assert run.status == 'succeeded' and run.final_output == {'value': 7}, run
    assert engine.store.load_workflow_snapshot(run.workflow_run_id)['_frozen_scripts'] == frozen


@pytest.mark.parametrize('missing', ['version', 'reference', 'payload_blob', 'code_sha256', 'side_blob'])
def test_explicit_pin_requires_exact_complete_history_even_if_current_code_matches(tmp_path, missing):
    _, _, row, _ = published_script(tmp_path)
    published = record_version(tmp_path, 'script', 'probe', payload=row, code=row['code'].encode(),
        side_files={'rows.json': 'frozen-rows'}, storage_revision=row['revision'],
        execution_definition_ref=row['execution_definition_ref'])['record']
    pin = frozen_pin(row, published)
    if missing == 'version':
        pin['version'] += 100
    elif missing == 'reference':
        pin['execution_definition_ref'] = 'f' * 64
    else:
        digest = published['side_blobs']['rows.json'] if missing == 'side_blob' else published[missing]
        (tmp_path / 'data/asset_evolution/blobs' / digest[:2] / digest).unlink()
    assert historical_pin_available(tmp_path, pin) is False
    runner = CountingRunner([result()])
    value = runtime.run_script_with_datasources(tmp_path, 'probe', expected_script_pin=pin, runner=runner)
    assert not value.ok and value.error_code == 'SCRIPT_REVISION_DRIFT'
    assert value.source_check['pin_resolution'] == {'mode': 'missing', 'reason': 'history_blob_missing'}
    assert not runner.calls


def test_exact_version_cannot_be_rebound_to_same_sha_different_reference(tmp_path):
    store, descriptor, row, old = published_script(tmp_path)
    store.save_script(replace(descriptor, io_contract={'outputs': ['later']}), CODE)
    new = latest_version(tmp_path, 'script', 'probe')
    pin = {**frozen_pin(row, old), 'execution_definition_ref': new['execution_definition_ref']}
    assert historical_pin_available(tmp_path, pin) is False
    runner = CountingRunner([result()])
    value = runtime.run_script_with_datasources(tmp_path, 'probe', expected_script_pin=pin, runner=runner)
    assert not value.ok and not runner.calls
    assert get_version(tmp_path, 'script', 'probe', old['version']) == old


def test_real_recurring_schedule_runs_again_after_normal_summary_fold(tmp_path):
    from agent_py_agent.tests.test_plan_w8_schedule_target import asset_case

    case = asset_case(tmp_path)
    repository = case.agent.scheduler_repository
    home = case.agent.home_paths.owner_home_dir
    initial = case.scripts.get_script('scheduled_script')
    published = latest_version(home, 'script', 'scheduled_script')
    frozen = []
    for due in (1000, 1600):
        case.agent.scheduler_service.enqueue_ready_runs(now=due)
        queued = repository.queued_runs(now=due)
        assert len(queued) == 1
        frozen.append(json.loads(json.dumps(queued[0])))
        case.scheduler.tick(now=due + 1)
        case.scheduler.tick(now=due + 2)
        history, errors = repository.history()
        assert errors == [] and len(history) == len(frozen)
        by_id = {row['run_id']: row for row in history}
        for before in frozen:
            row = by_id[before['run_id']]
            assert row['status'] == 'done', row
            for key in ('owner', 'job_id', 'scheduled_for', 'execution_definition_ref', 'execution_target'):
                assert row[key] == before[key]
            assert row['model_call_count'] == 0
            assert row['structured_result']['output_data'] == {'ok': True, 'value': 7}
        if due == 1000:
            # 真实runner已经非阻塞记摘要；仅显式折叠才改变canonical存储revision。
            summary = case.scripts.get_script('scheduled_script')
            assert summary['runtime_summary']['run_count'] == 1
            assert summary['revision'] == initial['revision']
            folded = case.scripts.fold_run_summaries('scheduled_script')
            assert folded['revision'] > initial['revision']
            assert folded['execution_definition_ref'] == initial['execution_definition_ref']
            assert latest_version(home, 'script', 'scheduled_script') == published
    assert len({row['run_id'] for row in frozen}) == 2
    assert case.agent.backend.prompts == []


@pytest.mark.parametrize('later_versions', [0, 24])
def test_exact_workflow_pin_reads_each_snapshot_once_without_history_scan(tmp_path, monkeypatch, later_versions):
    from agent_py_agent.agent.assets import revision_history
    from agent_py_agent.agent.workflows import pinned_definitions

    scripts, descriptor, _, published = published_script(tmp_path)
    WorkflowStore(tmp_path).save_workflow(WorkflowDefinition(AssetDescriptor('frozen', 'workflow', '冻结流程'), (
        WorkflowStepSpec('one', '第一步', 'deterministic_script', script_id='probe'),
        WorkflowStepSpec('two', '第二步', 'deterministic_script', script_id='probe'))))
    workflow = latest_version(tmp_path, 'workflow', 'frozen')
    for number in range(later_versions):
        scripts.save_script(descriptor, f'print({{"value": {number}}})\n')
    records, blobs = [], []
    original_get, original_blob = pinned_definitions.get_version, pinned_definitions.blob_get

    def read_record(home, kind, identifier, version):
        records.append((kind, identifier, version))
        return original_get(home, kind, identifier, version)

    def read_blob(home, digest):
        blobs.append(digest)
        return original_blob(home, digest)

    def no_scan(*args, **kwargs):
        pytest.fail('explicit version path must not load or scan the history index')

    monkeypatch.setattr(pinned_definitions, 'get_version', read_record)
    monkeypatch.setattr(pinned_definitions, 'blob_get', read_blob)
    monkeypatch.setattr(pinned_definitions, 'find_version', no_scan)
    monkeypatch.setattr(revision_history, '_load_index', no_scan)
    _, frozen = resolve_definition(tmp_path, 'frozen', workflow['version'])
    assert records == [('workflow', 'frozen', workflow['version']), ('script', 'probe', published['version'])]
    assert blobs == [workflow['payload_blob'], *workflow['side_blobs'].values(),
                     published['payload_blob'], published['code_sha256'], *published['side_blobs'].values()]
    assert frozen['one'] == frozen['two']


def open_handle_count():
    """POSIX数真实FD；Windows使用对应的进程句柄计数，不建立额外监测进程。"""
    if os.name == 'nt':
        import ctypes
        count = ctypes.c_ulong()
        kernel = ctypes.windll.kernel32
        assert kernel.GetProcessHandleCount(kernel.GetCurrentProcess(), ctypes.byref(count))
        return count.value
    return len(os.listdir('/dev/fd'))


def test_historical_execution_releases_handles_on_success_and_prepare_error(tmp_path, monkeypatch):
    from agent_py_agent.agent.workflows import script_runner

    store, descriptor, row, published = published_script(tmp_path)
    store.save_script(descriptor, 'print({"value": 8})\n')
    pin = frozen_pin(row, published)
    runner = ScriptRunner(tmp_path)
    # 先经过一次原生准备、执行和摘要路径，把一次性模块加载排除出泄漏计数。
    warm = runtime.run_script_with_datasources(tmp_path, 'probe', expected_script_pin=pin, runner=runner)
    assert warm.ok and warm.output_data == {'value': 7}, warm
    before = open_handle_count()
    success = runtime.run_script_with_datasources(tmp_path, 'probe', expected_script_pin=pin, runner=runner)
    assert success.ok and success.output_data == {'value': 7}
    assert open_handle_count() - before == 0
    prepared = []

    def unavailable(command, folder, environment, input_json, timeout, capture):
        prepared.append(Path(folder))
        raise OSError('simulated process preparation error')

    monkeypatch.setattr(script_runner, '_execute', unavailable)
    before = open_handle_count()
    failure = runtime.run_script_with_datasources(tmp_path, 'probe', expected_script_pin=pin, runner=runner)
    assert not failure.ok and len(prepared) == 1
    assert open_handle_count() - before == 0
    assert not prepared[0].exists() and not list((tmp_path / 'tmp/script_exec').iterdir())
    assert runtime.script_execution_record(tmp_path, 'probe', None) is None
    assert runtime.script_history_record(tmp_path, 'probe', None) is None
