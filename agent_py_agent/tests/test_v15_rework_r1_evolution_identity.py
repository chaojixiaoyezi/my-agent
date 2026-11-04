"""R1：公开工具、真实子进程、原冻结请求与学习 FIFO 的身份回归。"""
import hashlib
import json
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from dataclasses import replace
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.agent_core import _finalization_service as finalization
from agent_py_agent.agent.agent_core import _tool_loop_service as tool_loop
from agent_py_agent.agent.assets.datasource_store import DatasourceStore
from agent_py_agent.agent.assets.descriptor import AssetDescriptor, IOContract
from agent_py_agent.agent.assets.mutation_ledger import AssetMutationLedger, freeze_asset_mutations
from agent_py_agent.agent.assets.revision_history import get_version, latest_version
from agent_py_agent.agent.assets.script_store import ScriptStore
from agent_py_agent.agent.capability.asset_evolution import (
    AssetEvolutionService,
    evolution_family_fingerprint,
    find_evolution_family,
)
from agent_py_agent.agent.capability.asset_evolution_replay import evolution_descriptor
from agent_py_agent.agent.capability.asset_evolution_routes import (
    read_evolution_json,
    route_existing_script,
)
from agent_py_agent.agent.capability.skill_learning import SkillLearningService
from agent_py_agent.agent.capability.skill_learning_store import MAX_REQUEST_ATTEMPT_COUNT
from agent_py_agent.agent.tooling.scripts_tool import ScriptsTool
from agent_py_agent.agent.workflows.script_runner import ScriptRunner
from agent_py_agent.tests.test_skill_learning import _ctx, _output, _runtime

_SOURCE = '''import json, os, sys
inputs = json.load(sys.stdin)
rows = inputs.get("rows", [1])
output = {"execution_pid": os.getpid(), "source_report": {
    "stage": "probe+fetch", "discovered_sources": ["local-source"],
    "sources": [{"source_id": "local-source", "status": "ok",
                 "hit_count": len(rows), "http_status": None, "error_code": ""}]}}
if not inputs.get("historical_fail", True):
    output["result"] = len(rows)
print(json.dumps(output))
'''
_CURRENT = _SOURCE.replace('"historical_fail", True', '"fail", False')


def _case(tmp_path):
    """只使用本地进程和 Python 根守卫，不宣称验证了原生 OS 沙箱。"""
    runtime = _runtime(tmp_path, _output('skip'))
    home, work = runtime.home.owner_home_dir, tmp_path / 'work'
    work.mkdir()
    DatasourceStore(home).save_datasource({
        'datasource_id': 'local-source', 'kind': 'custom', 'title': '本地输入样本'})
    runner = ScriptRunner(home, workspace_root=work, use_sandbox=False, restrict_to_roots=True)
    tool = ScriptsTool(home, workspace_root=work, runner=runner)
    service = SkillLearningService(runtime.store, runtime.service.settings, runtime.service.runtime)
    agent = SimpleNamespace(root=work, home_paths=runtime.home,
                            config=SimpleNamespace(enable_self_learning=True))
    return SimpleNamespace(home=home, work=work, tool=tool, agent=agent,
                           service=service, runtime=runtime, store=ScriptStore(home))


def _wire(case, arguments):
    """客户端解析公开工具实际序列化结果，不手造脚本回执。"""
    result = case.tool.execute(arguments)
    value = json.loads(result.output)
    assert value == result.result_envelope
    return result, value


def _observe(case, state, arguments, result, call_id, *, run_id='capture'):
    params = SimpleNamespace(live_archive_state=state, run_id=run_id,
                             attempt_id='attempt-' + run_id, in_use_method_ids=())
    call = SimpleNamespace(tool_name='scripts', arguments=arguments, call_id=call_id)
    record = SimpleNamespace(params=params, call=call, result=result)
    tool_loop._observe_turn_asset_mutations(case.agent, record, {})


def _register(case, code=_SOURCE, *, inputs=(), state=None, run_id='capture'):
    args = {'action': 'register', 'script_id': 'sample', 'code': code,
            'descriptor': {'name': '身份检查', 'access_targets': [], 'credential_refs': [],
                'data_sources': [{'source_id': 'local-source', 'system': 'custom',
                                  'query_scope': {'access': 'read'}}],
                'io_contract': {'inputs': list(inputs), 'outputs': ['result', 'execution_pid']}}}
    result, value = _wire(case, args)
    assert result.ok, value
    if state is not None:
        _observe(case, state, args, result, 'register', run_id=run_id)
    return value


def _promote(case):
    result = route_existing_script(case.home, 'sample', _CURRENT, maintenance_mode='autonomous')
    assert result['status'] == 'promoted', result
    return case.store.get_script('sample')


def _run(case, *, version=None, inputs=None, state=None, call_id='execute', run_id='capture'):
    args = {'action': 'run', 'script_id': 'sample', 'inputs': inputs or {}}
    if version is not None:
        args['version'] = version
    result, value = _wire(case, args)
    assert value.get('output_data', {}).get('execution_pid') != os.getpid()
    assert value.get('output_data', {}).get('execution_pid'), value
    assert value['execution_definition_ref']
    if state is not None:
        before = deepcopy(value)
        _observe(case, state, args, result, call_id, run_id=run_id)
        assert json.loads(result.output) == before
    return args, result, value


def _enqueue(case, run_id):
    ctx = _ctx(run_id, attempt_id='attempt-' + run_id, tool_rounds=2,
               tool_runtime_evidence={}, archive_tool_calls=[])
    finalization._freeze_turn_asset_mutations(case.agent, ctx)
    assert ctx.tool_runtime_evidence['asset_evolution_run_id'] == run_id
    assert case.service.enqueue_from_finalize(ctx) == 'queued'
    return read_evolution_json(case.home, ('data', 'asset_evolution', 'requests', run_id + '.json'))


def _trial(case):
    return read_evolution_json(case.home, ('data', 'asset_evolution', 'trials', 'sample.json'))


@pytest.mark.parametrize('historical_ok', [False, True])
def test_historical_wire_receipts_cannot_count_against_promoted_trial(tmp_path, historical_ok):
    case, state = _case(tmp_path), {}
    _register(case)
    current = _promote(case)
    _, _, working = _run(case)
    assert working['ok'] is True
    old = get_version(case.home, 'script', 'sample', 1)
    for index in range(2):
        _, _, value = _run(case, version=1, inputs={'historical_fail': not historical_ok},
                           state=state, call_id=f'history-{index}', run_id='history')
        assert value['ok'] is historical_ok
        assert value['source_check']['query_health']['analysis_blocked'] is False
        assert value['execution_definition_ref'] == old['execution_definition_ref']
    request = _enqueue(case, 'history')
    assert len(request['executions']) == 2
    for row in request['executions']:
        assert row['execution']['sha256'] == old['code_sha256']
        assert row['execution']['execution_definition_ref'] == old['execution_definition_ref']
    assert case.service.run_pending().status == 'skipped'
    assert case.store.get_script('sample')['revision'] == current['revision']
    assert latest_version(case.home, 'script', 'sample')['version'] == 2
    assert _trial(case)['failures'] == 0 and _trial(case)['seen_calls'] == []
    assert case.runtime.backend.prompts == []


def test_current_failures_rollback_but_historical_success_does_not_reset_trial(tmp_path):
    case = _case(tmp_path)
    _register(case)
    current = _promote(case)
    assert _trial(case).get('promoted_definition_ref') == current['execution_definition_ref']
    for run_id, version, inputs in (
        ('current-a', None, {'fail': True}),
        ('old-success', 1, {'historical_fail': False}),
        ('current-b', None, {'fail': True}),
    ):
        _, _, value = _run(case, version=version, inputs=inputs, state={},
                           call_id=run_id, run_id=run_id)
        assert value['source_check']['query_health']['analysis_blocked'] is False
        _enqueue(case, run_id)
        assert case.service.run_pending().status == 'skipped'
        if run_id != 'current-b':
            assert _trial(case)['failures'] == 1
            assert case.store.get_script('sample')['code'] == _CURRENT
        duplicate = AssetEvolutionService(case.home).process(run_id)
        assert duplicate['duplicate'] is True and duplicate['notices'] == []
    assert case.store.get_script('sample')['code'] == _SOURCE
    assert latest_version(case.home, 'script', 'sample')['version'] == 3
    assert _trial(case)['active'] is False


@pytest.mark.parametrize('change', ['code', 'contract', 'summary'])
def test_edit_between_execution_and_observation_never_reattributes_bytes(tmp_path, change):
    case, state = _case(tmp_path), {}
    _register(case, _CURRENT, state=state)
    args, receipt, value = _run(case)
    before = case.store.get_script('sample')
    descriptor = evolution_descriptor(before)
    if change == 'contract':
        descriptor = replace(descriptor, io_contract=IOContract(outputs=('result',)))
    elif change == 'summary':
        descriptor = replace(descriptor, summary='新的说明')
    code = _CURRENT + '# later\n' if change == 'code' else _CURRENT
    case.store.save_script(descriptor, code, expected_revision=before['revision'])
    _observe(case, state, args, receipt, 'before-edit')
    execution = state['asset_mutations']['targets']['script:sample']['last_execution']
    assert execution['execution_definition_ref'] == value['execution_definition_ref']
    if change != 'summary':
        assert execution.get('verified') is False
        assert execution['sha256'] is None
    request = freeze_asset_mutations(case.home, state, run_id='intervening-edit')
    if change != 'summary':
        assert request['targets'] == []
    else:
        assert request['targets'][0]['verified'] is True


@pytest.mark.parametrize('change', ['contract', 'summary'])
def test_same_code_new_descriptor_supersedes_trial_before_failure_count(tmp_path, change):
    case = _case(tmp_path)
    _register(case)
    current = _promote(case)
    for index in range(2):
        _run(case, inputs={'fail': True}, state={}, call_id=f'failure-{index}', run_id=f'newer-{index}')
    descriptor = evolution_descriptor(current)
    if change == 'contract':
        descriptor = replace(descriptor, io_contract=IOContract(outputs=('result',)))
    else:
        descriptor = replace(descriptor, summary='较新的描述符')
    newer = case.store.save_script(descriptor, _CURRENT, expected_revision=current['revision'])
    for index in range(2):
        _enqueue(case, f'newer-{index}')
        assert case.service.run_pending().status == 'skipped'
    assert case.store.get_script('sample')['revision'] == newer['revision']
    assert latest_version(case.home, 'script', 'sample')['version'] == 3
    assert _trial(case)['active'] is False and _trial(case)['failures'] == 0


def test_workspace_source_keeps_precedence_and_cannot_fail_owner_trial(tmp_path):
    case, state = _case(tmp_path), {}
    _register(case)
    current = _promote(case)
    overlay = ScriptStore(case.home, workspace_root=case.work)
    workspace_code = _SOURCE + '# workspace\n'
    workspace = overlay.save_script(evolution_descriptor(current), workspace_code, target_scope='workspace')
    for index in range(2):
        _, _, value = _run(case, state=state, call_id=f'workspace-{index}', run_id='workspace')
        assert value['execution_definition_ref'] == workspace['execution_definition_ref']
        assert value['ok'] is False
    request = _enqueue(case, 'workspace')
    for row in request['executions']:
        assert row['execution']['sha256'] == hashlib.sha256(workspace_code.encode()).hexdigest()
        assert row['execution']['source_scope'] == 'workspace'
    assert case.service.run_pending().status == 'skipped'
    assert case.store.get_script('sample')['revision'] == current['revision']
    assert _trial(case)['failures'] == 0


def test_query_blocked_current_failure_is_not_a_trial_failure(tmp_path):
    case = _case(tmp_path)
    _register(case)
    _promote(case)
    for index in range(2):
        run_id = f'blocked-{index}'
        _, _, value = _run(case, inputs={'rows': [], 'fail': True}, state={}, call_id=run_id, run_id=run_id)
        assert value['source_check']['query_health']['analysis_blocked'] is True
        request = freeze_asset_mutations(case.home, case.agent._asset_mutation_states[(run_id, 'attempt-' + run_id)], run_id=run_id)
        assert request['evidence_tags'] == ['query_failed'] and request['executions'] == []
        assert AssetEvolutionService(case.home).process(run_id)['status'] == 'query_failed'
    assert _trial(case)['failures'] == 0


@pytest.mark.parametrize('inputs', [
    ['a', 'b'], [{'name': 'a', 'type': 'integer'}, {'type': 'string', 'name': 'b'}],
    ['a', {'name': 'b', 'default': {'nested': [1, True, None]}}],
    [None, True, 1, 1.5, ['a'], {'other': 'value'}],
])
def test_family_fingerprint_is_total_and_order_insensitive_for_accepted_inputs(inputs):
    contract = IOContract(inputs=tuple(inputs)).to_dict()
    try:
        first = evolution_family_fingerprint(_CURRENT, contract)
        reordered = evolution_family_fingerprint(_CURRENT, {**contract, 'inputs': inputs[::-1]})
    except (TypeError, ValueError) as exc:
        pytest.fail(f'accepted input contract must have a fingerprint: {type(exc).__name__}')
    assert first == reordered and len(first) == 64


@pytest.mark.parametrize('left,right', [
    ({'inputs': [{'name': 'a', 'type': 'string'}]}, {'inputs': [{'name': 'a', 'type': 'integer'}]}),
    ({'inputs': [{'name': 'a', 'required': False}]}, {'inputs': [{'name': 'a', 'required': True}]}),
    ({'outputs': ['result'], 'output_types': {'result': 'string'}},
     {'outputs': ['result'], 'output_types': {'result': 'integer'}}),
    ({'outputs': ['result'], 'nullable_outputs': []}, {'outputs': ['result'], 'nullable_outputs': ['result']}),
])
def test_family_fingerprint_preserves_material_contract_differences(left, right):
    assert evolution_family_fingerprint(_CURRENT, left) != evolution_family_fingerprint(_CURRENT, right)


def _queued_structured_target(tmp_path):
    case, state = _case(tmp_path), {}
    fields = [{'name': 'rows', 'type': 'array'}, {'name': 'fail', 'type': 'boolean'}]
    _register(case, _CURRENT, inputs=fields, state=state, run_id='structured')
    _, _, value = _run(case, state=state, run_id='structured')
    assert value['ok'] is True
    request = _enqueue(case, 'structured')
    assert len(request['targets']) == 1 and request['targets'][0]['verified'] is True
    assert case.service.enqueue_from_finalize(_ctx('next-valid')) == 'queued'
    paths = case.runtime.store.pending_requests()
    assert len(paths) == 2
    return case, request, paths[0], paths[1]


def test_structured_input_real_freeze_and_fifo_head_complete_before_next_request(tmp_path):
    case, request, head, next_path = _queued_structured_target(tmp_path)
    assert find_evolution_family(case.home, evolution_family_fingerprint(
        _CURRENT, request['targets'][0]['io_contract'])) == 'sample'
    result = case.service.run_pending(expected_request_path=head)
    assert result.status == 'skipped' and not head.exists()
    assert case.runtime.store.pending_requests() == [next_path]
    assert case.runtime.backend.prompts == []
    assert case.service.run_pending(expected_request_path=next_path).status == 'skipped'
    assert not case.runtime.store.pending_requests() and len(case.runtime.backend.prompts) == 1


def test_deterministic_evolution_failure_has_bounded_retry_and_correlated_terminal_event(tmp_path, monkeypatch):
    case, _, head, next_path = _queued_structured_target(tmp_path)
    original = case.runtime.store.read_request(head)

    def fail_route(self, request):
        raise TypeError('deterministic contract processing failure')

    monkeypatch.setattr(AssetEvolutionService, '_route_request', fail_route)
    for attempt in range(1, MAX_REQUEST_ATTEMPT_COUNT + 1):
        # 重新装配消费者，验证 attempts 来自原请求而不是进程内计数。
        service = SkillLearningService(case.runtime.store, case.service.settings, case.service.runtime)
        try:
            result = service.run_pending(expected_request_path=head)
        except TypeError:
            pytest.fail('deterministic evolution failure escaped without FIFO progress')
        assert result.request_key == original['request_key']
        assert result.code == 'SKILL_LEARNING_EVOLUTION_FAILED'
        if attempt < MAX_REQUEST_ATTEMPT_COUNT:
            assert result.status == 'retry'
            saved = case.runtime.store.read_request(head)
            assert saved['attempts'] == attempt
            assert {key: value for key, value in saved.items() if key != 'attempts'} == {
                key: value for key, value in original.items() if key != 'attempts'}
        else:
            assert result.status == 'failed' and not head.exists()
    event = case.runtime.store.events(limit=1)[0]
    assert (event['event'], event['code'], event['reason']) == (
        'failed', 'SKILL_LEARNING_EVOLUTION_FAILED', 'TypeError')
    assert event['request_key'] == original['request_key'] and event['run_id'] == original['run_id']
    assert case.runtime.store.pending_requests() == [next_path]
    assert case.service.run_pending(expected_request_path=next_path).status == 'skipped'
    assert not case.runtime.store.pending_requests()


def test_historical_effective_permissions_are_verified_without_history_scan(tmp_path, monkeypatch):
    from agent_py_agent.agent.assets import mutation_ledger, revision_history

    case = _case(tmp_path)
    _register(case)
    before = case.store.get_script('sample')
    descriptor = evolution_descriptor(before)
    descriptor = replace(descriptor, data_sources=(replace(descriptor.data_sources[0], query_scope={}),))
    case.store.save_script(descriptor, _SOURCE, expected_revision=before['revision'])
    historical = latest_version(case.home, 'script', 'sample')
    _promote(case)
    args, receipt, value = _run(case, version=historical['version'])
    # 历史执行使用当前与原权限求交；实际引用可能不同于未求交的发布引用。
    assert value['execution_definition_ref'] != historical['execution_definition_ref']
    versions, reads = [], []
    get = mutation_ledger.get_version
    read = mutation_ledger.read_bytes_beneath

    def exact_version(home, kind, asset_id, version):
        versions.append((kind, asset_id, version))
        return get(home, kind, asset_id, version)

    def bounded_blob(home, parts, *, max_bytes):
        reads.append((parts, max_bytes))
        return read(home, parts, max_bytes=max_bytes)

    def no_enumeration(*args, **kwargs):
        pytest.fail('capture must not enumerate accumulated version history')

    monkeypatch.setattr(mutation_ledger, 'get_version', exact_version)
    monkeypatch.setattr(mutation_ledger, 'read_bytes_beneath', bounded_blob)
    monkeypatch.setattr(revision_history, '_names', no_enumeration)
    state = {}
    _observe(case, state, args, receipt, 'effective-history')
    fact = state['asset_mutations']['targets']['script:sample']['last_execution']
    assert fact['verified'] is True and fact['sha256'] == historical['code_sha256']
    assert fact['execution_definition_ref'] == value['execution_definition_ref']
    assert fact['published_definition_ref'] == historical['execution_definition_ref']
    assert versions == [('script', 'sample', historical['version'])]
    assert len(reads) == 2
    assert [limit for _, limit in reads] == [AssetMutationLedger.max_capture_bytes, 4194304]
    assert AssetMutationLedger.max_targets == 32 and AssetMutationLedger.max_facts_per_target == 64


def test_duplicate_original_call_in_two_requests_is_idempotent(tmp_path):
    case = _case(tmp_path)
    _register(case)
    _promote(case)
    args, receipt, _ = _run(case, inputs={'fail': True})
    for run_id in ('delivery-a', 'delivery-b'):
        _observe(case, {}, args, receipt, 'original-call', run_id=run_id)
        request = _enqueue(case, run_id)
        assert request['executions'][0]['execution']['call_id'] == 'original-call'
        assert case.service.run_pending().status == 'skipped'
        assert _trial(case)['failures'] == 1 and _trial(case)['seen_calls'] == ['original-call']
        assert case.store.get_script('sample')['code'] == _CURRENT


def test_restore_cas_does_not_overwrite_edit_after_trial_identity_check(tmp_path, monkeypatch):
    case, state = _case(tmp_path), {}
    _register(case)
    _promote(case)
    for index in range(2):
        _run(case, inputs={'fail': True}, state=state, call_id=f'cas-{index}', run_id='cas')
    request = _enqueue(case, 'cas')
    saved, original = [], ScriptStore.save_script

    def edit_before_restore(self, descriptor, code, **options):
        if self.owner_home == case.home and code == _SOURCE and not saved:
            current = self.get_script('sample')
            newer = replace(evolution_descriptor(current), summary='恢复提交前的新说明')
            saved.append(original(self, newer, _CURRENT, expected_revision=current['revision']))
        return original(self, descriptor, code, **options)

    monkeypatch.setattr(ScriptStore, 'save_script', edit_before_restore)
    assert case.service.run_pending().status == 'retry'
    assert len(saved) == 1 and case.store.get_script('sample')['revision'] == saved[0]['revision']
    assert case.store.get_script('sample')['code'] == _CURRENT
    assert _trial(case)['failures'] == 1
    assert case.service.run_pending().status == 'skipped'
    assert _trial(case)['active'] is False and _trial(case)['failures'] == 1
    after = read_evolution_json(case.home, ('data', 'asset_evolution', 'requests', 'cas.json'))
    assert after['executions'] == request['executions']
    assert latest_version(case.home, 'script', 'sample')['version'] == 3


def test_owner_fifo_has_one_active_processor_with_deterministic_interleaving(tmp_path, monkeypatch):
    case, _, head, next_path = _queued_structured_target(tmp_path)
    entered, release = threading.Event(), threading.Event()
    lock, counts = threading.Lock(), {'active': 0, 'peak': 0}
    original = AssetEvolutionService._route_request

    def held_route(self, request):
        with lock:
            counts['active'] += 1
            counts['peak'] = max(counts['peak'], counts['active'])
        try:
            entered.set()
            assert release.wait(10), 'test must release original processing'
            return original(self, request)
        finally:
            with lock:
                counts['active'] -= 1

    monkeypatch.setattr(AssetEvolutionService, '_route_request', held_route)
    with ThreadPoolExecutor(max_workers=1) as pool:
        first = pool.submit(case.service.run_pending, expected_request_path=head)
        try:
            assert entered.wait(10)
            other = SkillLearningService(case.runtime.store, case.service.settings, case.service.runtime)
            assert other.run_pending(expected_request_path=head).status == 'busy'
            assert case.runtime.store.pending_requests() == [head, next_path]
        finally:
            release.set()
        assert first.result(10).status == 'skipped'
    assert counts == {'active': 0, 'peak': 1}
    assert case.runtime.store.pending_requests() == [next_path]
    assert case.service.run_pending(expected_request_path=next_path).status == 'skipped'


@pytest.mark.parametrize('kind', ['outcome', 'context', 'tool', 'bounded', 'interrupt', 'cancel', 'io', 'quota', 'still-running', 'runtime'])
def test_evolution_control_or_uncertain_outcome_keeps_original_request(tmp_path, monkeypatch, kind):
    from asyncio import CancelledError

    from agent_py_agent.agent.backends.bounded_call import BoundedCallExitUnconfirmedError
    from agent_py_agent.agent.common.request_execution_ports import (
        RequestExecutionContextUnavailable,
        RequestExecutionOutcomeUncertainError,
    )
    from agent_py_agent.agent.common.tool_write_context import ToolWriteContextUnavailable
    from agent_py_agent.agent.memory_store.curator_backend import CuratorModelStillRunningError
    from agent_py_agent.agent.user_space.owner_quota import OwnerQuotaUnavailable

    case, _, head, next_path = _queued_structured_target(tmp_path)
    error = {
        'outcome': RequestExecutionOutcomeUncertainError(
            {'id': 'request', 'execution_attempt_id': 'attempt', 'lease_epoch': 1, 'lease_owner': 'attempt'}, 'evolution'),
        'context': RequestExecutionContextUnavailable('original execution missing'),
        'tool': ToolWriteContextUnavailable('original write source missing'),
        'bounded': BoundedCallExitUnconfirmedError('original-call', 'worker_exit_unconfirmed'),
        'interrupt': InterruptedError('original processing interrupted'),
        'cancel': CancelledError(),
        'io': OSError('original storage unavailable'),
        'quota': OwnerQuotaUnavailable('authoritative usage unavailable'),
        'still-running': CuratorModelStillRunningError('original worker still running'),
        'runtime': RuntimeError('unclassified processing outcome'),
    }[kind]

    def refuse(self, request):
        raise error

    monkeypatch.setattr(AssetEvolutionService, '_route_request', refuse)
    before = head.read_bytes()
    ledger = case.runtime.store.ledger_path
    events = ledger.read_bytes() if ledger.exists() else None
    if kind == 'quota':
        result = case.service.run_pending(expected_request_path=head)
        assert (result.status, result.code) == ('failed', 'SKILL_LEARNING_STORAGE_LIMIT')
    elif kind == 'still-running':
        assert case.service.run_pending(expected_request_path=head).status == 'busy'
    else:
        with pytest.raises(type(error)) as raised:
            case.service.run_pending(expected_request_path=head)
        assert raised.value is error
    assert head.read_bytes() == before
    assert case.runtime.store.pending_requests() == [head, next_path]
    assert (ledger.read_bytes() if ledger.exists() else None) == events
    assert case.runtime.backend.prompts == []
    with case.runtime.store.run_lock():
        assert case.runtime.store.read_request(head)['attempts'] == 0


def test_real_restore_committed_readback_failure_keeps_request_and_attempts(tmp_path, monkeypatch):
    from agent_py_agent.agent.assets import AssetPostCommitReadbackError

    case, state = _case(tmp_path), {}
    _register(case)
    current = _promote(case)
    for index in range(2):
        _run(case, inputs={'fail': True}, state=state, call_id=f'readback-{index}', run_id='readback')
    frozen = _enqueue(case, 'readback')
    head = case.runtime.store.pending_requests()[0]
    before = head.read_bytes()
    ledger = case.runtime.store.ledger_path
    events = ledger.read_bytes() if ledger.exists() else None
    original, committed = ScriptStore._readback_after_commit, []

    def fail_projection(record):
        raise OSError('single post-commit projection read unavailable')

    def post_commit_readback(self, asset_id):
        if self.owner_home != case.home:
            return original(self, asset_id)
        committed.append(asset_id)
        # 保留原Store提交、读回与异常包装，只在已提交后的展示读取注入一次真实I/O故障。
        with monkeypatch.context() as patch:
            patch.setattr(self, '_project_record', fail_projection)
            return original(self, asset_id)

    monkeypatch.setattr(ScriptStore, '_readback_after_commit', post_commit_readback)
    fd_before = _descriptor_count()
    with pytest.raises(AssetPostCommitReadbackError) as raised:
        case.service.run_pending(expected_request_path=head)
    assert isinstance(raised.value.__cause__, OSError)
    assert committed == ['sample']
    restored = case.store.get_script('sample')
    assert restored['code'] == _SOURCE and restored['revision'] > current['revision']
    assert head.read_bytes() == before and case.runtime.store.read_request(head)['attempts'] == 0
    assert (ledger.read_bytes() if ledger.exists() else None) == events
    assert read_evolution_json(case.home, ('data', 'asset_evolution', 'requests', 'readback.json')) == frozen
    assert _trial(case)['active'] is True
    with case.runtime.store.run_lock():
        assert case.runtime.store.pending_requests() == [head]
    fd_after = _descriptor_count()
    print(json.dumps({'scenario': 'committed-readback-unknown', 'fd_before': fd_before,
                      'fd_after': fd_after, 'fd_delta': fd_after - fd_before}))
    assert fd_after == fd_before


def _descriptor_count():
    """POSIX 直接观察 FD；Windows 使用同进程句柄计数，不修改运行时注册表。"""
    if os.name == 'nt':
        import ctypes
        count = ctypes.c_ulong()
        kernel = ctypes.windll.kernel32
        assert kernel.GetProcessHandleCount(kernel.GetCurrentProcess(), ctypes.byref(count))
        return count.value
    with os.scandir('/dev/fd') as entries:
        return sum(1 for _ in entries)


@pytest.mark.parametrize('fail', [False, True])
def test_real_execution_observe_and_process_release_descriptors(tmp_path, fail):
    case = _case(tmp_path)
    _register(case, _CURRENT)
    _run(case)  # 先完成解释器/摘要仓库的常规初始化，再测相同入口。
    before = _descriptor_count()
    state = {}
    _, _, value = _run(case, inputs={'fail': fail}, state=state, call_id='measured-call')
    assert value['ok'] is not fail
    assert value['source_check']['query_health']['analysis_blocked'] is False
    request = freeze_asset_mutations(case.home, state, run_id='fd-measured')
    assert request['executions'][0]['execution']['verified'] is True
    assert AssetEvolutionService(case.home).process('fd-measured')['status'] == 'processed'
    after = _descriptor_count()
    print(json.dumps({'scenario': 'contract-failure' if fail else 'success',
                      'fd_before': before, 'fd_after': after, 'fd_delta': after - before}))
    assert after - before == 0
