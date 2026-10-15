"""只使用临时RuntimeDB/生产API的S07非作者审查；安全预期失败即复现缺口。"""
import time

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


# LLM: 只用生产API登记真实Task→Attempt和原操作，避免SQL造出的不可能权限状态。
# 函数用途: 建立原未知/终态操作供隔离回归，不访问供应商。
def original(tmp_path, status='unknown'):
    repo=RuntimeRepository(tmp_path/'runtime.db')
    chain=repo.record_run_creation(owner_id='owner-a', goal='合成', conversation_task_id='root-a',
        thread_id='thread-a', run_id='run-a', role='assistant')
    store=ManagedOperationStore(repo)
    holder=new_tool_operation_holder()
    request=ToolOperationClaimRequest(owner_id='owner-a',run_id='run-a',task_id='root-a',
        operation_id='op-original',tool='synthetic_write',args_hash='same-args',idempotency_key='same-key',
        idempotency_scope='operation',idempotency_namespace='same-namespace',holder=holder,
        lease_expires_at=time.time()+60,attempt_id=str(chain['attempt_id']))
    claim=store.claim_tool_operation(request)
    store.finish_tool_operation(ToolOperationCompletionRequest(owner_id='owner-a',run_id='run-a',
        operation_id='op-original',holder_id=holder.holder_id,generation=claim.record.generation,
        status=status,result={'ok':status=='succeeded','output':'original root private synthetic result'}))
    return repo,store,chain,request,claim


@pytest.mark.parametrize('action',['settle','reopen'])
@pytest.mark.parametrize('layer',['agent','task'])
@pytest.mark.parametrize('status',['cancelled','done','failed'])
def test_cancelled_same_attempt_reconciliation_must_not_settle_or_reopen(tmp_path,action,layer,status):
    repo,store,chain,original_request,previous=original(tmp_path)
    holder=new_tool_operation_holder()
    request=ToolOperationReconciliationClaimRequest(owner_id='owner-a',run_id='run-a',operation_id='op-original',
        expected_generation=previous.record.generation,holder=holder,lease_expires_at=time.time()+60,
        authority_attempt_id=original_request.attempt_id,task_id='root-a',tool_name='synthetic_write',args_hash='same-args')
    claimed=store.claim_tool_operation_reconciliation(request)
    assert claimed.action=='reconcile' and not claimed.record.reconciliation_only
    if layer=='agent':
        repo.settle_agent_run(agent_run_id=str(chain['agent_run_id']),status=status,attempt_id=original_request.attempt_id)
    else:
        repo.settle_task_run_terminal(task_run_id=str(chain['task_run_id']),status=status)
    rejected=False
    try:
        if action=='settle':
            store.finish_tool_operation(ToolOperationCompletionRequest(owner_id='owner-a',run_id='run-a',operation_id='op-original',
                holder_id=holder.holder_id,generation=claimed.record.generation,status='succeeded',result={'ok':True}))
        else:
            store.reopen_tool_operation_after_reconciliation(ToolOperationReopenRequest(owner_id='owner-a',run_id='run-a',
                operation_id='op-original',expected_generation=claimed.record.generation,holder=holder,
                lease_expires_at=time.time()+60,source_ref='synthetic://provider-proof'))
    except RuntimeConflictError:
        rejected=True
    assert rejected, 'terminal authority retained permission to '+action
    record=store.get_tool_operation(owner_id='owner-a',run_id='run-a',operation_id='op-original')
    assert record.status=='unknown' and record.generation==claimed.record.generation


@pytest.mark.parametrize('status',['succeeded','failed'])
@pytest.mark.parametrize('owner,run',[('owner-a','run-b'),('owner-b','run-b'),('owner-a','run-a')])
def test_terminal_replay_must_keep_original_root_authority(tmp_path,status,owner,run):
    repo,store,_,_,_=original(tmp_path,status=status)
    other=repo.record_run_creation(owner_id=owner,goal='another synthetic root',conversation_task_id='root-b',
        thread_id='thread-b',run_id=run,role='assistant')
    holder=new_tool_operation_holder()
    request=ToolOperationClaimRequest(owner_id=owner,run_id=run,task_id='root-b',operation_id='op-original',
        tool='synthetic_write',args_hash='same-args',idempotency_key='same-key',idempotency_scope='operation',
        idempotency_namespace='same-namespace',holder=holder,lease_expires_at=time.time()+60,attempt_id=str(other['attempt_id']))
    try:
        claimed=store.claim_tool_operation(request)
    except ToolOperationOwnershipError:
        return
    assert claimed.action=='conflict', ('foreign-root terminal replay',claimed.action,claimed.record.result)


def test_same_root_current_attempt_still_replays_original_terminal(tmp_path):
    repo,store,chain,request,_=original(tmp_path,status='succeeded')
    from dataclasses import replace
    current=repo.create_attempt(str(chain['agent_run_id']))
    replay=store.claim_tool_operation(replace(request,attempt_id=str(current['attempt_id']),holder=new_tool_operation_holder()))
    assert replay.action=='replay' and replay.record.result['ok'] is True
