"""未知工具收口只对原身份纯读对账；业务回执与工具UNKNOWN不互相覆盖。"""
import hashlib
import json
from dataclasses import replace
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.action_protocol import RunScope
from agent_py_agent.agent.agent_core._tool_loop_service import _mark_unknown_outcome_halt
from agent_py_agent.agent.conversation.business_access import BusinessError
from agent_py_agent.agent.conversation.business_receipt_rows import ReceiptRows
from agent_py_agent.agent.gateway_parts import business_api as api
from agent_py_agent.agent.local_storage import LocalStore
from agent_py_agent.agent.tooling.models import ToolHandlerOutcome
from agent_py_agent.tests import test_business_model_tools as business
from agent_py_agent.tests import test_tool_operation_idempotency as operations
from agent_py_agent.tests._tool_runtime_harness import canonical_test_call

agent = business.agent
KEY = 'unknown-receipt-inspect-0001'
PARAMS = {'name': 'receipt-target', 'purpose': 'synthetic'}


def setup_receipt(agent):
    context, services = api.local_business_context(agent, model=True)
    actor = services.actor(context)
    result = services.spaces.create(PARAMS, actor=actor, idempotency_key=KEY)
    return services.spaces, actor, result


def state_hashes(root):
    return {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in (root / 'data/business').rglob('*') if p.is_file()}


def test_receipt_read_exact_identity_and_target_is_pure_and_never_recovers(agent, monkeypatch):
    service, actor, result = setup_receipt(agent)
    for name in ('recover', 'migrate', 'write', 'payload', 'has_history'):
        monkeypatch.setattr(ReceiptRows, name, lambda *_a, **_k: pytest.fail('查账不能恢复或写入'))
    before = state_hashes(service.owner_root)
    read = service.inspect_receipt('spaces.create', PARAMS, actor=actor, idempotency_key=KEY)
    assert read == {'operation_id': next((service.owner_root / 'data/business/receipts').glob('*.json')).stem,
                    'state': 'completed', 'target_refs': [result['space_id']], 'error': None}
    assert state_hashes(service.owner_root) == before


@pytest.mark.parametrize('mutation', ['missing', 'args', 'subject', 'owner', 'read_revoked', 'write_revoked'])
def test_receipt_mismatch_or_revocation_cannot_confirm_or_leak(agent, mutation):
    service, actor, _ = setup_receipt(agent)
    params, key = dict(PARAMS), KEY
    if mutation == 'missing':
        key += '-absent'
    elif mutation == 'args':
        params['purpose'] = 'changed'
    elif mutation == 'subject':
        actor = replace(actor, subject_id='other-actor')
    elif mutation == 'owner':
        actor = replace(actor, owner_id='local/other')
    elif mutation == 'read_revoked':
        actor = replace(actor, authorize=lambda action, _: action != 'spaces.get')
    else:
        actor = replace(actor, authorize=lambda action, _: action != 'spaces.create')
    before = state_hashes(service.owner_root)
    if mutation in ('missing', 'subject'):
        assert service.inspect_receipt('spaces.create', params, actor=actor, idempotency_key=key) is None
    else:
        with pytest.raises(BusinessError):
            service.inspect_receipt('spaces.create', params, actor=actor, idempotency_key=key)
    assert state_hashes(service.owner_root) == before


def test_pending_journal_stays_pending_and_does_not_materialize(agent):
    service, actor, result = setup_receipt(agent)
    path = next((service.owner_root / 'data/business/receipts').glob('*.json'))
    receipt = json.loads(path.read_text())
    pending = {'schema': 'business-catalog-transaction.v1', 'owner_id': service.owner_id,
               'key': path.stem, 'receipt': receipt['receipt'], 'state': service._read()}
    journal = service.owner_root / 'data/business/.catalog-transaction.json'
    journal.write_text(json.dumps(pending))
    before = state_hashes(service.owner_root)
    found = service.inspect_receipt('spaces.create', PARAMS, actor=actor, idempotency_key=KEY)
    assert found['state'] == 'pending' and found['target_refs'] == [result['space_id']]
    assert state_hashes(service.owner_root) == before


def test_corrupt_owner_receipt_is_not_missing_or_success(agent):
    service, actor, _ = setup_receipt(agent)
    path = next((service.owner_root / 'data/business/receipts').glob('*.json'))
    data = json.loads(path.read_text())
    data['owner_id'] = 'local/other'
    path.write_text(json.dumps(data))
    with pytest.raises(BusinessError, match='STORE_UNAVAILABLE'):
        service.inspect_receipt('spaces.create', PARAMS, actor=actor, idempotency_key=KEY)


@pytest.mark.parametrize('mutation', ['none', 'owner', 'run', 'task', 'attempt', 'args', 'snapshot'])
def test_unknown_halt_reads_exact_original_operation_without_running_handler(tmp_path, mutation, monkeypatch):
    store = LocalStore(tmp_path / 'operations.db', enable_fts=False)
    tool = operations._CountingTool(result=ToolHandlerOutcome('counting_write', False,
        'unknown', error_code='TOOL_OPERATION_OUTCOME_UNKNOWN', effect_outcome='unknown'))
    registry = operations._registry(tmp_path, store, tool)
    result = operations._execute(registry, operations._call('run-1', 'call-1', 1))
    snapshot = registry.runtime_snapshot(run_id='run-1')
    call = canonical_test_call(snapshot, 'counting_write', {'value': 1}, call_id='call-1',
        attempt_id='attempt:run-1', operation_id='tool_call:call-1',
        idempotency_key=result.operation.idempotency_key)
    scope = RunScope(request_id='request-run-1', run_id='run-1', task_id='run-1',
        attempt_id='attempt:run-1', owner_id='owner-a')
    if mutation in ('owner', 'run', 'task', 'attempt'):
        scope = replace(scope, **{dict(owner='owner_id', run='run_id', task='task_id', attempt='attempt_id')[mutation]: 'other'})
    elif mutation == 'args':
        call = replace(call, arguments={'value': 2})
    elif mutation == 'snapshot':
        snapshot = replace(snapshot, run_id='other', snapshot_hash='')
    params = SimpleNamespace(run_scope=scope, tool_runtime_snapshot=snapshot,
        repeated_failure_halt=None, unknown_outcome_halt=None, tool_context=[])
    host = SimpleNamespace(home_paths=SimpleNamespace(owner_id='owner-a'), tools=registry)
    tool.inspect_operation_receipt = lambda *_: pytest.fail('同名任意hook不授查账能力')
    queries = []
    original_get = store.get_tool_operation
    def counted_get(**kwargs):
        queries.append(kwargs)
        return original_get(**kwargs)
    monkeypatch.setattr(store, 'get_tool_operation', counted_get)
    record = SimpleNamespace(params=params, call=call, result=result)
    _mark_unknown_outcome_halt(host, record)
    _mark_unknown_outcome_halt(host, record)
    assert len(queries) <= 1

    facts = json.loads(params.tool_context[0].split('\n', 1)[1])
    if mutation != 'none':
        assert facts['lookup'] == 'unavailable' and 'operation_id' not in facts
        assert tool.calls == 1 and params.unknown_outcome_halt is not None
        return
    assert facts['operation_id'] == 'tool_call:call-1' and facts['canonical_status'] == 'unknown'
    assert facts['replay_permitted'] is False and 'business_receipt' not in facts
    assert tool.calls == 1 and params.unknown_outcome_halt is not None
    assert store.get_tool_operation(owner_id='owner-a', run_id='run-1', operation_id='tool_call:call-1').status == 'unknown'


def test_only_original_snapshot_business_handler_can_issue_receipt_inspection(agent):
    service, _, result = setup_receipt(agent)
    snapshot = agent.tools.runtime_snapshot(run_id='receipt-snapshot')
    runtime = snapshot.runtime('business_spaces_create')
    request = {'action': 'spaces.create', 'request_id': 'original-receipt-request',
        'params': PARAMS, 'idempotency_key': KEY}
    before = state_hashes(service.owner_root)
    found = api.inspect_registered_business_receipt(runtime, request)
    assert found['state'] == 'completed' and found['target_refs'] == [result['space_id']]
    assert state_hashes(service.owner_root) == before
    fake = SimpleNamespace(handler=SimpleNamespace(
        inspect_operation_receipt=lambda *_: pytest.fail('不能按名字接受伪handler')))
    assert api.inspect_registered_business_receipt(fake, request) is None


def test_receipt_lookup_ignores_unrelated_bad_history_without_directory_scan(agent, monkeypatch):
    service, actor, result = setup_receipt(agent)
    directory = service.owner_root / 'data/business/receipts'
    for index in range(128):
        (directory / (hashlib.sha256(str(index).encode()).hexdigest() + '.json')).write_text('broken unrelated history')
    from agent_py_agent.agent.conversation import business_receipt_rows

    monkeypatch.setattr(business_receipt_rows, 'list_names_beneath',
        lambda *_: pytest.fail('精确回执对账不能扫描历史'))
    found = service.inspect_receipt('spaces.create', PARAMS, actor=actor, idempotency_key=KEY)
    assert found['target_refs'] == [result['space_id']] and found['state'] == 'completed'


def test_no_change_result_without_permanent_receipt_does_not_confirm(agent):
    service, actor, result = setup_receipt(agent)
    params = {'space_id': result['space_id']}
    service.active_references = lambda *_: [{'ref': 'task-current', 'kind': 'task', 'reason_code': 'active'}]
    blocked = service.change_state('spaces.archive', params, actor=actor, expected_revision=0,
        idempotency_key='archive-blocked-no-write-0001')
    assert blocked['blockers'] and blocked['space']['state'] == 'active'
    before = state_hashes(service.owner_root)
    assert service.inspect_receipt('spaces.archive', params, actor=actor, expected_revision=0,
        idempotency_key='archive-blocked-no-write-0001') is None
    assert state_hashes(service.owner_root) == before


def test_different_action_or_expected_revision_does_not_match_original_fingerprint(agent):
    service, actor, result = setup_receipt(agent)
    with pytest.raises(BusinessError, match='IDEMPOTENCY_CONFLICT'):
        service.inspect_receipt('spaces.archive', {'space_id': result['space_id']}, actor=actor,
            expected_revision=0, idempotency_key=KEY)
    with pytest.raises(BusinessError, match='IDEMPOTENCY_CONFLICT'):
        service.inspect_receipt('spaces.create', PARAMS, actor=actor, expected_revision=0, idempotency_key=KEY)
