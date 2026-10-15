# LLM: UNKNOWN跨attempt只授予原操作只读核对权，原执行attempt身份不改；结算继续校验新holder/generation与当前活跃fence。
# 模块用途: 在同一owner/root的现有操作账事务内核验恢复者，防止旧attempt结算、跨任务接管或盲重执行。
from __future__ import annotations

import json

from ..local_storage.tool_operations import ToolOperationOwnershipError
from .operations import OP_UNKNOWN, RUN_STATUS_LEGACY_CREATED, RuntimeConflictError


# LLM: 身份从同事务JOIN读取；跨attempt必须显式携带完整原tool/args/task与当前attempt，缺字段不能猜当前调用者。
# 函数用途: 取得原操作及可核验的当前活跃恢复身份，保持原操作的执行attempt不变。
def load_reconciliation_authority(conn, request):
    row = conn.execute(
        'SELECT op.*, ar.workspace_epoch AS _workspace_epoch, ar.run_id AS _run_id, '
        'ar.current_attempt_id AS _current_attempt_id, ar.current_attempt_generation AS _current_attempt_generation, '
        'ar.status AS _agent_run_status, at.status AS _attempt_status, tr.status AS _task_run_status, t.owner_id AS _owner_id, t.task_id AS _task_id '
        'FROM tool_operations op JOIN agent_runs ar ON ar.agent_run_id=op.agent_run_id '
        'JOIN agent_attempts original ON original.attempt_id=op.attempt_id AND original.agent_run_id=ar.agent_run_id '
        'AND original.attempt_generation=op.attempt_generation '
        'JOIN agent_attempts at ON at.attempt_id=ar.current_attempt_id AND at.agent_run_id=ar.agent_run_id '
        'AND at.attempt_generation=ar.current_attempt_generation '
        'JOIN task_runs tr ON tr.task_run_id=ar.task_run_id JOIN tasks t ON t.task_id=tr.task_id '
        'WHERE op.operation_id=?', (request.operation_id,)).fetchone()
    if row is None or row['_owner_id'] != request.owner_id or row['_run_id'] != request.run_id:
        raise ToolOperationOwnershipError('原操作owner/run身份不匹配')
    if row['_task_run_status'] in {'done', 'failed', 'cancelled'}:
        raise ToolOperationOwnershipError('原任务执行已终止，不能接管核对')
    payload = json.loads(row['outcome_json'])
    cross_attempt = row['attempt_id'] != row['_current_attempt_id'] or row['attempt_generation'] != row['_current_attempt_generation']
    if cross_attempt or request.authority_attempt_id:
        _assert_recovery_request(row, payload, request)
    elif row['_agent_run_status'] not in RUN_STATUS_LEGACY_CREATED or row['_attempt_status'] != 'running':
        raise ToolOperationOwnershipError('原操作核对执行已停止')
    return row, cross_attempt


# LLM: 当前attempt、准确root/task、原工具和args hash全部匹配才可核对；不接受仅凭operation编号的恢复。
# 函数用途: 拒绝旧attempt、取消任务和跨身份请求，不改变任何持久状态。
def _assert_recovery_request(row, payload, request):
    if (not request.authority_attempt_id or request.authority_attempt_id != row['_current_attempt_id']
            or not request.task_id or request.task_id != row['_task_id'] or payload.get('task_id') != row['_task_id']
            or not request.tool_name or request.tool_name != row['operation_type']
            or not request.args_hash or request.args_hash != payload.get('args_hash')
            or row['_agent_run_status'] not in RUN_STATUS_LEGACY_CREATED or row['_attempt_status'] != 'running'
            or row['_task_run_status'] in {'done', 'failed', 'cancelled'}):
        raise ToolOperationOwnershipError('原操作核对缺少当前活跃attempt或准确任务/工具/参数事实')


# LLM: 跨attempt核对只生成CAS标记与新操作代数，不迁移原执行身份或清除UNKNOWN；原workspace epoch仍保留。
# 函数用途: 构造只读核对租约的当前权威投影，供同事务资源锁与后续结算使用。
def reconciliation_authority_marker(row, request, cross_attempt):
    return {'authority_attempt_id':request.authority_attempt_id or row['attempt_id'],
        'authority_attempt_generation':int(row['_current_attempt_generation']),
        'workspace_epoch':int(row['_workspace_epoch']), 'read_only':cross_attempt}


# LLM: 结算和重开共用活跃attempt/task/epoch门；同attempt也不能因未置reconciliation_only而跳过取消fence。
# 函数用途: 在原事务验证当前执行权及核对标记，返回是否获准以跨attempt只读恢复身份结算。
def assert_reconciliation_completion(conn, row, payload):
    read_only = payload.get('reconciliation_only') is True
    marker = payload.get('reconciliation_claim') or {}
    if (read_only and not marker) or (marker and (
            row['status'] != OP_UNKNOWN or marker.get('generation') != row['tool_operation_generation']
            or marker.get('read_only') is not read_only)):
        raise RuntimeConflictError('恢复原操作状态或核对代数不匹配')
    authority = conn.execute(
        'SELECT ar.current_attempt_id, ar.current_attempt_generation, ar.workspace_epoch, ar.status AS agent_run_status, '
        'at.status AS attempt_status, tr.status AS task_run_status '
        'FROM agent_runs ar JOIN task_runs tr ON tr.task_run_id=ar.task_run_id JOIN agent_attempts at ON at.attempt_id=ar.current_attempt_id '
        'AND at.agent_run_id=ar.agent_run_id AND at.attempt_generation=ar.current_attempt_generation '
        'WHERE ar.agent_run_id=?', (row['agent_run_id'],)).fetchone()
    expected_attempt = marker.get('authority_attempt_id') if marker else row['attempt_id']
    expected_generation = marker.get('authority_attempt_generation') if marker else row['attempt_generation']
    expected_epoch = marker.get('workspace_epoch') if marker else payload.get('workspace_epoch')
    if (authority is None or expected_attempt != authority['current_attempt_id']
            or expected_generation != authority['current_attempt_generation']
            or expected_epoch != authority['workspace_epoch']
            or authority['task_run_status'] in {'done', 'failed', 'cancelled'}):
        raise RuntimeConflictError('操作已失去当前attempt/epoch或任务已停止')
    from .managed_operation_store import AuthorityContextMissing, _require_running_authority
    try:
        _require_running_authority(authority, '', expected_attempt)
    except AuthorityContextMissing:
        raise RuntimeConflictError('操作执行或attempt已停止') from None
    return read_only


# LLM: 终态/在途重放也必须读取原operation的真实owner/root及当前调用attempt所属agent_run，不能由请求投影冒充。
# 函数用途: 在返回已有结果前阻止跨owner、跨任务或同名run的其它执行链获取旧操作事实。
def assert_operation_request_identity(conn, row, request):
    actual = conn.execute(
        'SELECT ar.run_id, t.owner_id, t.task_id FROM agent_runs ar '
        'JOIN task_runs tr ON tr.task_run_id=ar.task_run_id JOIN tasks t ON t.task_id=tr.task_id '
        'JOIN agent_attempts caller ON caller.agent_run_id=ar.agent_run_id AND caller.attempt_id=? '
        'WHERE ar.agent_run_id=?', (request.attempt_id, row['agent_run_id'])).fetchone()
    if (actual is None or actual['owner_id'] != request.owner_id or actual['run_id'] != request.run_id
            or (request.task_id and actual['task_id'] != request.task_id)):
        raise ToolOperationOwnershipError('已有操作owner/run/root与当前调用者不匹配')


# LLM: 原账同run/task、tool/args/namespace的UNKNOWN不能靠新call编号绕过；未来周期不同run仍独立。
# 函数用途: 在新操作占位前找出准确等价未知记录，只允许走原操作核对。
def equivalent_managed_unknown(conn, agent_run_id, request):
    if request.idempotency_scope != 'operation':
        return None
    return conn.execute(
        "SELECT * FROM tool_operations WHERE agent_run_id=? AND operation_type=? AND status='UNKNOWN' "
        "AND json_extract(outcome_json, '$.args_hash')=? AND json_extract(outcome_json, '$.idempotency_scope')='operation' "
        "AND json_extract(outcome_json, '$.idempotency_namespace')=? ORDER BY created_at,operation_id LIMIT 1",
        (agent_run_id, request.tool, request.args_hash, request.idempotency_namespace)).fetchone()
