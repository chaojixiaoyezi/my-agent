"""原UNKNOWN只读核对跨attempt接管，不迁移原执行身份或授予重执行。"""
import json
import time
from dataclasses import replace

import pytest

from agent_py_agent.agent.local_storage.tool_operations import (
    ToolOperationClaimRequest,
    ToolOperationCompletionRequest,
    ToolOperationOwnershipError,
    ToolOperationReconciliationClaimRequest,
    ToolOperationReopenRequest,
    new_tool_operation_holder,
)
from agent_py_agent.agent.runtime_db.managed_operation_store import ManagedOperationStore
from agent_py_agent.agent.runtime_db.repository import RuntimeConflictError, RuntimeRepository


# LLM: 登记、attempt换代与原操作领取/UNKNOWN都走生产API，只用临时RuntimeDB和合成编号。
# 函数用途: 建立旧attempt未确认副作用及新活跃恢复者，不改SQL绕过执行权门。
def _case(tmp_path):
    repo = RuntimeRepository(tmp_path/'runtime.db')
    chain = repo.record_run_creation(owner_id='owner-a', goal='合成核对', conversation_task_id='root-task',
        thread_id='thread-one', run_id='run-one', role='assistant')
    old_attempt = str(chain['attempt_id'])
    store = ManagedOperationStore(repo)
    old_holder = new_tool_operation_holder()
    claimed = store.claim_tool_operation(ToolOperationClaimRequest(owner_id='owner-a', run_id='run-one', task_id='root-task',
        operation_id='op-original', tool='synthetic_write', args_hash='args-original', idempotency_key='key-original',
        idempotency_scope='operation', idempotency_namespace='synthetic_write', holder=old_holder,
        lease_expires_at=time.time()+900, attempt_id=old_attempt))
    store.finish_tool_operation(ToolOperationCompletionRequest(owner_id='owner-a', run_id='run-one', operation_id='op-original',
        holder_id=old_holder.holder_id, generation=claimed.record.generation, status='unknown',
        result={'ok':False}, unknown_reason='synthetic-response-lost'))
    current = repo.create_attempt(str(chain['agent_run_id']))
    request = ToolOperationReconciliationClaimRequest(owner_id='owner-a', run_id='run-one', operation_id='op-original',
        expected_generation=claimed.record.generation, holder=new_tool_operation_holder(), lease_expires_at=time.time()+900,
        authority_attempt_id=str(current['attempt_id']), task_id='root-task', tool_name='synthetic_write', args_hash='args-original')
    return repo, store, chain, old_attempt, old_holder, claimed.record, request


def test_new_attempt_claim_preserves_original_attempt_and_blocks_old_settlement(tmp_path):
    repo, store, _chain, old_attempt, old_holder, previous, request = _case(tmp_path)
    claimed = store.claim_tool_operation_reconciliation(request)
    assert claimed.action == 'reconcile' and claimed.record.reconciliation_only
    assert claimed.record.generation == previous.generation+1
    with repo._runtime_connection() as conn:
        row = conn.execute('SELECT * FROM tool_operations WHERE operation_id=?', ('op-original',)).fetchone()
    assert row['attempt_id'] == old_attempt and row['status'] == 'UNKNOWN'
    marker = json.loads(row['outcome_json'])['reconciliation_claim']
    assert marker['authority_attempt_id'] == request.authority_attempt_id and marker['read_only']
    with pytest.raises(RuntimeConflictError):
        store.finish_tool_operation(ToolOperationCompletionRequest(owner_id='owner-a', run_id='run-one', operation_id='op-original',
            holder_id=old_holder.holder_id, generation=previous.generation, status='succeeded', result={'ok':True}))
    settled = store.finish_tool_operation(ToolOperationCompletionRequest(owner_id='owner-a', run_id='run-one', operation_id='op-original',
        holder_id=request.holder.holder_id, generation=claimed.record.generation, status='succeeded', result={'ok':True}))
    assert settled.status == 'succeeded'


@pytest.mark.parametrize('field,value', [('owner_id','other-owner'), ('run_id','other-run'), ('task_id','other-root'),
    ('tool_name','other-tool'), ('args_hash','other-args'), ('authority_attempt_id','old-or-guessed-attempt')])
def test_reconciliation_rejects_wrong_identity_without_mutating_original(tmp_path, field, value):
    repo, store, _chain, _old, _holder, previous, request = _case(tmp_path)
    with pytest.raises(ToolOperationOwnershipError):
        store.claim_tool_operation_reconciliation(replace(request, **{field:value}))
    with repo._runtime_connection() as conn:
        row = conn.execute('SELECT * FROM tool_operations WHERE operation_id=?', ('op-original',)).fetchone()
    assert row['tool_operation_generation'] == previous.generation and row['status'] == 'UNKNOWN'
    assert not json.loads(row['outcome_json']).get('reconciliation_claim')


@pytest.mark.parametrize('timing', ['before_claim','after_claim'])
def test_cancelled_authority_cannot_claim_or_settle_old_unknown(tmp_path, timing):
    repo, store, chain, _old, _holder, _previous, request = _case(tmp_path)
    claimed = store.claim_tool_operation_reconciliation(request) if timing == 'after_claim' else None
    repo.settle_agent_run(agent_run_id=str(chain['agent_run_id']), status='cancelled', attempt_id=request.authority_attempt_id)
    if claimed:
        with pytest.raises(RuntimeConflictError):
            store.finish_tool_operation(ToolOperationCompletionRequest(owner_id='owner-a', run_id='run-one', operation_id='op-original',
                holder_id=request.holder.holder_id, generation=claimed.record.generation, status='succeeded', result={'ok':True}))
    else:
        with pytest.raises(ToolOperationOwnershipError):
            store.claim_tool_operation_reconciliation(request)


def test_readonly_takeover_cannot_reopen_write_even_with_source_ref(tmp_path):
    _repo, store, _chain, _old, _holder, _previous, request = _case(tmp_path)
    claimed = store.claim_tool_operation_reconciliation(request)
    with pytest.raises(ToolOperationOwnershipError):
        store.reopen_tool_operation_after_reconciliation(ToolOperationReopenRequest(owner_id='owner-a', run_id='run-one',
            operation_id='op-original', expected_generation=claimed.record.generation, holder=request.holder,
            lease_expires_at=time.time()+900, source_ref='synthetic-provider-proof'))


def test_newer_attempt_fences_already_claimed_recovery(tmp_path):
    repo, store, chain, _old, _holder, _previous, request = _case(tmp_path)
    claimed = store.claim_tool_operation_reconciliation(request)
    repo.create_attempt(str(chain['agent_run_id']))
    with pytest.raises(RuntimeConflictError):
        store.finish_tool_operation(ToolOperationCompletionRequest(owner_id='owner-a', run_id='run-one', operation_id='op-original',
            holder_id=request.holder.holder_id, generation=claimed.record.generation, status='succeeded', result={'ok':True}))


def test_concurrent_recovery_claim_keeps_one_holder_generation(tmp_path):
    _repo, store, _chain, _old, _holder, _previous, request = _case(tmp_path)
    first = store.claim_tool_operation_reconciliation(request)
    second = store.claim_tool_operation_reconciliation(replace(request, holder=new_tool_operation_holder()))
    assert first.action == 'reconcile'
    assert second.action in {'conflict','in_flight'}
    assert second.record.holder_id == first.record.holder_id
