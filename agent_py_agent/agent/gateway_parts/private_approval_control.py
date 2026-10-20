# LLM: SQLite结果码读取兼容Python3.10，缺真实错误码仍拒绝重试；私卡只运输原点击；W仅重试提交前只读校验的真实SQLITE_BUSY，禁止重投input/业务，UNKNOWN仍不因退休变completed。
# 模块用途: 签卡点击接回原执行者；实际本机源的固定receipt写完整raw观察，Count0/W不变，沿唯一控制账保幂等/unknown，不新Agent。
from __future__ import annotations

import hashlib
import time
from dataclasses import replace

from ..contracts.tool_approval import ToolApprovalDecision
from ..conversation.control_commands import ConversationControlResult
from ..runtime_errors import DataCorruptionError, runtime_error_report
from ..user_space.owner_resolver import (
    OwnerIdentity,
    owner_identity_from_config,
    resolve_owner_home,
)
from . import control_operation_service as ops
from .control_service import GatewayControlScope
from .io import write_json_file_atomic
from .multiprocess_global_write import GlobalWriteError

_KIND = 'private_tool_approval'
_ACTION_VERSION = 'gateway_private_tool_approval.v1'


# LLM: 只验证同账结构，不验证MAC或当前scope；schema/action是输入而非授权，禁止fields/Params/writer及任意附加字段。
# 函数用途: 拒绝控制账里不完整或越界的私卡决定载荷。
def require_private_approval_action(action):
    if (not isinstance(action, dict)
            or set(action) != {'schema_version', 'reference', 'decision', 'message_id', 'chat_id'}
            or action.get('schema_version') != _ACTION_VERSION or action.get('decision') not in {'approved', 'denied'}
            or any(type(action.get(key)) is not str or not action[key] or len(action[key]) > maximum
                   for key, maximum in [('reference', 8192), ('message_id', 512), ('chat_id', 512)])):
        raise DataCorruptionError('private approval action is invalid')
    return action


# LLM: scope只投影已MAC核验的原owner/actor/thread；无权限位/执行源，W仍读取原实际运行对象。
# 函数用途: 为同一原卡生成稳定控制身份和完整点击摘要。
def _approval_receipt_input(source):
    body, payload = source.body, source.payload
    action = require_private_approval_action({'schema_version': _ACTION_VERSION,
        **{key: body[key] for key in ('reference', 'decision', 'message_id', 'chat_id')}})
    scope = GatewayControlScope(source.actor, source.channel, payload['scope']['thread_id'],
        metadata={'message_id': 'private-approval:' + hashlib.sha256(body['reference'].encode()).hexdigest(),
                  'channel_chat_type': 'p2p', 'channel_chat_id': body['chat_id']}, resolved_owner=source.owner)
    operation_id, _old_digest = ops.gateway_control_operation_identity(scope, _KIND)
    digest = ops._control_operation_input_digest(scope, _KIND, version=5, private_approval=action)
    return scope, action, operation_id, digest


# LLM: 实际source.perform仍原入口；op锁只试取，原本机prepared走固定raw叶，远端dispatch/原input冻结和重放不改。
# 函数用途: 只在原C短门中准备同一私卡控制回执。
def _prepare(source, scope, action, operation_id, digest):
    from .multiprocess_control_forward import prepare_remote_control

    with ops.try_gateway_control_operation_transition(source.paths, operation_id) as acquired:
        if not acquired:
            raise GlobalWriteError('private_approval_transition_busy', 'private_interaction')
        current = ops.read_gateway_control_operation(source.paths, operation_id, read_only=True)
        if current is not None:
            if current.input_digest != digest:
                raise ops.GatewayControlOperationConflict('private approval input conflicts with original receipt')
            return current
        receipt = ops._new_control_operation_receipt(scope, _KIND, _KIND, operation_id, digest, private_approval=action)
        receipt = replace(receipt, conversation_thread_id=source.payload['scope']['thread_id'])
        _require_delivered_approval(source._resources[0].store, source.control.home_root, receipt)
        if source.owner == OwnerIdentity.local_main() and not _actual_local_agent(source):
            return None
        ops._validate_control_operation_receipt(receipt)
        forwarded = prepare_remote_control(None, source.paths, receipt, fresh=True, control_context=source.control, blocking=False)
        if forwarded is not None:
            return forwarded
        _write_original_local_receipt(source, source.paths, receipt, blocking=False)
        return receipt


# LLM: 仅真实SimpleAgent及完全相同canonical owner可接本机执行；None/资源host/字段相同Namespace都不提供实际RunParams。
# 函数用途: 只认C原本已经运行的本机业务实例，不冷建或借用外来owner。
def _actual_local_agent(source):
    agent = source.server.agent
    return agent if _actual_approval_agent(agent, source.owner) else None


# LLM: 只核实际SimpleAgent与原canonical home/config同源；资源身份不授执行权，scope仍由原决定器读取。
# 函数用途: 拒绝冷资源host、外owner或漂移路径，避免回到foreign Agent物化。
def _actual_approval_agent(agent, owner):
    from ..core import SimpleAgent
    if not isinstance(agent, SimpleAgent) or owner_identity_from_config(agent.config) != owner:
        return False
    home = agent.home_paths
    resolved = resolve_owner_home(home.root, owner)
    return (home.owner_provider == owner.provider and home.owner_kind == owner.owner_kind
            and home.owner_id == resolved.owner_id and home.owner_home_dir == resolved.home_dir)


# LLM: C只受理原点击；本机同actual source传固定receipt叶，实际W/本机waiter才应用输入，不造Agent或从MAC恢复执行许可。
# 函数用途: 沿原控制账投递审批输入并返回真实受理状态。
def execute_coordinator_private_approval(source):
    from .private_interaction_transaction import _PrivateInteractionTransaction
    if type(source) is not _PrivateInteractionTransaction:
        raise GlobalWriteError('private_source_changed', 'private_interaction')
    source.require_current()
    if source.payload['kind'] != 'tool_approval' or source.body['decision'] not in {'approved', 'denied'} or 'fields' in source.body:
        raise ValueError('private approval decision is invalid')
    scope, action, operation_id, digest = _approval_receipt_input(source)
    receipt = source.perform(lambda: _prepare(source, scope, action, operation_id, digest))
    if receipt is None:
        return {'ok': False, 'status': 'waiting', 'error_code': 'COORDINATOR_BUSINESS_NOT_READY', 'terminal': False}
    if receipt.dispatch is None:
        agent = _actual_local_agent(source)
        if agent is None:
            return {'ok': False, 'status': 'waiting', 'error_code': 'COORDINATOR_BUSINESS_NOT_READY', 'terminal': False}
        receipt = source.perform(lambda: _submit_local(agent, source.paths, receipt, blocking=False, source=source))
    return private_approval_status(receipt)


# LLM: 原Count0 HTTP已验证MAC/actor/锁/admin后只桥原paths/真实Agent；相同receipt/input，不安装或复制运行上下文。
# 函数用途: 修复跨线程私人审批回调，原同步API仍走原决定器。
def execute_count0_private_approval(agent, paths, payload, body, *, channel, actor_id):
    owner = OwnerIdentity(**payload['owner'])
    if not _actual_approval_agent(agent, owner):
        raise ValueError('private approval actual owner agent is unavailable')
    action = require_private_approval_action({'schema_version': _ACTION_VERSION,
        **{key: body[key] for key in ('reference', 'decision', 'message_id', 'chat_id')}})
    scope = GatewayControlScope(actor_id, channel, payload['scope']['thread_id'],
        metadata={'message_id': 'private-approval:' + hashlib.sha256(body['reference'].encode()).hexdigest(),
                  'channel_chat_type': 'p2p', 'channel_chat_id': body['chat_id']}, resolved_owner=owner)
    operation_id, _ = ops.gateway_control_operation_identity(scope, _KIND)
    digest = ops._control_operation_input_digest(scope, _KIND, version=5, private_approval=action)
    with ops.gateway_control_operation_transition(paths, operation_id):
        receipt = ops.read_gateway_control_operation(paths, operation_id, read_only=True)
        if receipt is None:
            receipt = ops._new_control_operation_receipt(scope, _KIND, _KIND, operation_id, digest, private_approval=action)
            receipt = replace(receipt, conversation_thread_id=payload['scope']['thread_id'])
            _require_delivered_approval(agent.conversation_store, agent.home_paths.root, receipt)
            write_json_file_atomic(ops.gateway_control_operation_path(paths, operation_id), receipt.to_dict())
        elif receipt.input_digest != digest:
            raise ops.GatewayControlOperationConflict('private approval input conflicts with original receipt')
    return private_approval_status(_submit_local(agent, paths, receipt))


# LLM: 原op锁在permission锁外；实际签卡source只透传固定receipt叶，prepared先executing、input仅一次，Count0缺省原body，unknown不重投。
# 函数用途: 将本机点击写入原permission输入并有界观察结果。
def _submit_local(agent, paths, receipt, *, blocking=True, source=None):
    from contextlib import nullcontext
    transition = (nullcontext(True) if blocking else ops.try_gateway_control_operation_transition(paths, receipt.operation_id))
    outer = ops.gateway_control_operation_transition(paths, receipt.operation_id) if blocking else nullcontext()
    with outer, transition as acquired:
        if not acquired:
            raise GlobalWriteError('private_approval_transition_busy', 'private_interaction')
        current = ops.read_gateway_control_operation(paths, receipt.operation_id, read_only=True)
        if current != receipt:
            return current
        if receipt.state == 'prepared':
            current = replace(receipt, state='executing', updated_at=time.time())
            if source is None:
                write_json_file_atomic(ops.gateway_control_operation_path(paths, receipt.operation_id), current.to_dict(), **({} if blocking else {"blocking": False}))
            else:
                _write_original_local_receipt(source, paths, current, blocking=blocking)
            try:
                _submit_approval_input(agent, current)
            except BaseException as error:
                _settle_input_failure_locked(agent, paths, current, error, blocking=blocking, **({} if source is None else {"source": source}))
                raise
        updated = reconcile_private_approval_locked(paths, current, agent=agent)
        if updated != current:
            if source is None:
                write_json_file_atomic(ops.gateway_control_operation_path(paths, receipt.operation_id), updated.to_dict(), **({} if blocking else {"blocking": False}))
            else:
                _write_original_local_receipt(source, paths, updated, blocking=blocking, expected= current)
        return updated


# LLM: 原W已核P5/实例及pool.pin真实Agent；只读校验最多三次/总15ms等待，input仍只提交一次，不借控制线程TLS批准。
# 函数用途: 让控制Future立即完成输入受理，原poll继续短观察同一个规范行。
def execute_worker_private_approval(agent, receipt):
    if not _actual_approval_agent(agent, ops._receipt_owner_identity(receipt)):
        raise ValueError('private approval actual owner agent is unavailable')
    _submit_approval_input(agent, receipt, retry_read_busy=True)
    return None


# LLM: 调用者仍持原op锁与实际来源；默认无重试，只有W显式缩小到只读校验；permission/input写绝不进入重试。
# 函数用途: 在原permission transition内保存同回执输入，重复不改写、冲突明确拒绝。
def _submit_approval_input(agent, receipt, *, retry_read_busy=False):
    from ..common.json_io import locked_json_path
    from ..common.json_io import write_json_file_atomic as write_row
    from ..conversation.agent_tool_approval import _transition_path
    action = require_private_approval_action(receipt.private_approval)
    body = {key: action[key] for key in ('reference', 'decision', 'message_id', 'chat_id')}
    _payload, locked = _validated_approval_input(agent, receipt, body, retry_read_busy=retry_read_busy)
    if locked is not None:
        raise ValueError('private approval user identity is unavailable')
    path, _scope, _request, _row = _permission_record(agent.conversation_store, agent.home_paths.root, receipt)
    with locked_json_path(_transition_path(path), blocking=False):
        _path, _scope, _request, row = _permission_record(agent.conversation_store, agent.home_paths.root, receipt)
        _require_delivered_approval(agent.conversation_store, agent.home_paths.root, receipt)
        expected = _decision_input(receipt)
        if row.get('decision_input') not in (None, expected):
            raise ops.GatewayControlOperationConflict('private approval already has different input')
        if row.get('decision_input') is None:
            write_row(path, {**row, 'decision_input': expected}, blocking=False)


# LLM: 仅W提交前的纯只读完整校验可重试实际primary SQLITE_BUSY；每次重核MAC/expiry/密码/admin，不包input写或Root内消费。
# 函数用途: 仅凭真实整数错误码短暂等待原个人锁库恢复可读，Python3.10缺属性时原样失败，最多三次与5/10ms等待；耗尽及其它错误保持原异常，默认C/Count0不等待。
def _validated_approval_input(agent, receipt, body, *, retry_read_busy=False):
    import sqlite3

    from ..concurrency.interrupt import wait_interruptibly
    from .private_interaction_service import validated_private_interaction_from_config

    delays = (0.0, 0.005, 0.010) if retry_read_busy else (0.0,)
    for attempt, delay in enumerate(delays):
        if delay:
            wait_interruptibly(delay)
        try:
            return validated_private_interaction_from_config(agent.home_paths.root, agent.config, body,
                channel=receipt.channel, actor_id=receipt.user_id)
        except sqlite3.OperationalError as error:
            code = getattr(error, 'sqlite_errorcode', None)
            # SQLite固定协议码BUSY=5；Python3.10未导出模块常量，不能按错误文字或版本补造异常码。
            if type(code) is not int or code & 0xff != 5 or attempt == len(delays) - 1:
                raise


# LLM: 此schema只引用原完整receipt，不含批准/用户角色/运行参数；摘要由原v5规范实现验证。
# 函数用途: 生成唯一决定输入的原回执锚点。
def _decision_input(receipt):
    return {'operation_id': receipt.operation_id, 'input_digest': receipt.input_digest}


# LLM: cancel参数仅用于已提交事实的过期MAC只读验证，不执行取消；原owner/scope/request/transport全部精确核对，不从JSON猜路径。
# 函数用途: 用原Store路径leaf读取同一签名审批记录，供提交、等待和只读对账共用。
def _permission_record(store, home_root, receipt):
    from ..common.json_io import read_json_object_report
    from ..conversation.agent_tool_approval import (
        _approval_request,
        _decision_from_payload,
        _record_matches,
        approval_path_for_store,
    )
    from ..conversation.tool_approval_scope import ToolApprovalScope
    from ..user_space.interaction_refs import verify_interaction_reference
    action = require_private_approval_action(receipt.private_approval)
    payload = verify_interaction_reference(home_root, action['reference'], channel=receipt.channel,
                                           actor_id=receipt.user_id, decision='cancel')
    if (payload.get('kind') != 'tool_approval' or OwnerIdentity(**payload['owner']) != ops._receipt_owner_identity(receipt)
            or payload['scope']['thread_id'] != receipt.conversation_id
            or payload['scope']['thread_id'] != receipt.conversation_thread_id):
        raise ValueError('private approval original identity changed')
    scope = ToolApprovalScope(**payload['scope'], agent_name='', active=False)
    path = approval_path_for_store(store, scope, payload['permission_id'])
    report = read_json_object_report(path, context='private.approval.original')
    row = report.payload
    if report.load_error is not None or not row:
        raise FileNotFoundError('private approval original record is unavailable')
    request = _approval_request(row.get('request'))
    transport = row.get('im_control') or {}
    if (not _record_matches(row, scope, request) or request.permission_id != payload['permission_id']
            or any(transport.get(key) != action[key] for key in ('reference', 'message_id', 'chat_id'))):
        raise ValueError('private approval original record changed')
    prior = _decision_from_payload(row)
    if (row.get('status') not in {'pending', 'decided'} or (row.get('status') == 'decided'
            and (prior is None or prior.permission_id != request.permission_id))):
        raise ValueError('private approval original decision is invalid')
    return path, scope, request, row


# LLM: 仅从真实Agent或原C配置资源取得同一Store；冷C不物化Agent、不建owner/会话，foreign覆盖遵原runtime_paths。
# 函数用途: 为既有回执的只读事实对账打开原owner审批账。
def _receipt_resources(receipt, *, agent=None, control=None):
    if agent is not None:
        if not _actual_approval_agent(agent, ops._receipt_owner_identity(receipt)):
            raise ValueError('private approval actual owner agent is unavailable')
        return agent.conversation_store, agent.home_paths.root
    from ..owner_scoped_pool import _config_with_owner
    from ..user_space.home_layout import home_paths
    from ..user_space.owner_resolver import home_paths_with_owner
    from ..user_space.runtime_paths import resolve_runtime_paths_for_agent
    from .control_plane import GatewayControlPlane
    from .owner_conversation_store import owner_conversation_store_from_config
    if type(control) is not GatewayControlPlane:
        raise ValueError('private approval original control is unavailable')
    owner, base = ops._receipt_owner_identity(receipt), home_paths(control.home_root)
    home = home_paths_with_owner(base, resolve_owner_home(control.home_root, owner))
    config = _config_with_owner(control.config, owner)
    if owner != owner_identity_from_config(control.config):
        names = resolve_runtime_paths_for_agent(control.config, base.owner_home_dir, base).paths
        config = replace(config, **{key: '' for key in names if hasattr(config, key)})
    return owner_conversation_store_from_config(config, home, initialize=False), control.home_root


# LLM: 调用者持原op锁；完整同input决定才投影，UNKNOWN+waiter_retirement永不completed，旧用户决定/旧行不补origin。
# 函数用途: 将原waiter结果推进原回执，损坏或缺失证据保unknown。
def reconcile_private_approval_locked(paths, receipt, *, agent=None, control=None):
    from ..common.json_io import locked_json_path
    from ..conversation.agent_tool_approval import _decision_from_payload, _transition_path
    if receipt.state == 'completed' or receipt.state == 'prepared':
        return receipt
    try:
        store, root = _receipt_resources(receipt, agent=agent, control=control)
        path, _scope, _request, row = _permission_record(store, root, receipt)
        with locked_json_path(_transition_path(path), blocking=False):
            _path, _scope, _request, row = _permission_record(store, root, receipt)
            if row.get('decision_input') != _decision_input(receipt):
                return receipt
            decision = _decision_from_payload(row)
            if decision is None or (receipt.state == 'terminal_unknown' and row.get('decision_origin') == 'waiter_retirement'):
                return receipt
            result = {'ok': decision.decision in {'approved', 'denied'} and not decision.reason_code, 'decision': decision.decision,
                      'run_id': row['run_id'], 'permission_id': decision.permission_id,
                      **({'reason_code': decision.reason_code} if decision.reason_code else {}),
                      **({'clicked_by': row['decision_by']} if row.get('decision_by') else {}),
                      **({'error_code': row['decision_input_error_code']} if row.get('decision_input_error_code') else {})}
            return replace(receipt, state='completed', result=_decision_result(result).to_dict(), error={}, updated_at=time.time())
    except BlockingIOError:
        return receipt
    except (FileNotFoundError, ValueError) as error:
        return replace(receipt, state='terminal_unknown', error=runtime_error_report(error, context='private.approval.observe'), updated_at=time.time())


# LLM: 原publish同线程局部callback不是权限；实际scope/端口仍逐次核验，wait退出即关闭，不执行旧Context/复制Params。
# 函数用途: 保存原waiter应用输入的本地回调和唯一退出开关，不建立全局句柄表。
def approval_input_callbacks(agent, handle):
    import os
    import threading

    from .multiprocess_execution import current_worker_execution
    from .paths import gateway_paths, gateway_paths_from_root
    from .request_binding import GatewayTaskBindingWriter
    execution = current_worker_execution()
    transition = None
    writer = getattr(getattr(agent, '_current_run_params', None), 'conversation_task_binding_callback', None)
    if execution is not None:
        claim = execution.claim if hasattr(execution, 'claim') else execution.binding.claim
        from .request_binding import GatewayActiveTurnTransition
        transition = GatewayActiveTurnTransition(claim.path, claim.request_id, claim.execution_attempt_id)
        paths = gateway_paths_from_root(claim.path.parent.parent.parent)
    elif isinstance(writer, GatewayTaskBindingWriter):
        paths = gateway_paths_from_root(writer.request_path.parent.parent.parent)
    else:
        paths = gateway_paths(agent)
    pid, thread, active = os.getpid(), threading.get_ident(), [True]

    # LLM: callback仅在原等待线程活寿命内批准消费；纯退休已分离，不受op锁忙阻塞或借本callback恢复执行权。
    # 函数用途: 让实际调用线程核验并应用自己的那一条用户输入。
    def consume():
        if not active[0] or pid != os.getpid() or thread != threading.get_ident():
            raise ValueError('private approval original waiter is unavailable')
        from ..common.json_io import read_json_object_report
        row = read_json_object_report(handle.path, context='private.approval.input').payload
        raw = row.get('decision_input')
        if raw is None:
            return None
        if not isinstance(raw, dict) or set(raw) != {'operation_id', 'input_digest'}:
            raise ValueError('private approval decision input is invalid')
        if execution is not None:
            from .multiprocess_background_execution import _uncertain_execution
            try:
                if current_worker_execution() is not execution:
                    raise ValueError('private approval original execution changed')
                execution.lifetime.require_original(execution)
            except (AttributeError, ValueError):
                raise _uncertain_execution(transition, 'tool_approval_decision') from None
        from ..common.request_execution_ports import gateway_short_operation
        return gateway_short_operation('tool_approval_decision', lambda: _consume_input_locked(agent, handle, paths, raw))

    # LLM: 只关闭这次原handle局部callback，不能清账、释放claim或让后来的waiter重新授权旧input。
    # 函数用途: 在原wait finally使迟到调用失效。
    def close():
        active[0] = False
    return consume, close


# LLM: 批准仍原执行短门→op→permission，unknown先赢不应用，已决定先赢只读；此入口不再承担纯退休写入。
# 函数用途: 仅原等待线程在所有实际权限门通过后处理同一个输入。
def _consume_input_locked(agent, handle, paths, raw):
    from contextlib import ExitStack

    from ..common.json_io import locked_json_path
    from ..conversation.agent_tool_approval import (
        _decision_from_payload,
        _record_matches,
        _transition_path,
    )
    with ops.try_gateway_control_operation_transition(paths, raw['operation_id']) as acquired:
        if not acquired:
            return None
        receipt = ops.read_gateway_control_operation(paths, raw['operation_id'], read_only=True)
        if receipt is None or receipt.private_approval is None or raw != _decision_input(receipt):
            raise ValueError('private approval original receipt changed')
        with ExitStack() as locks:
            try:
                locks.enter_context(locked_json_path(_transition_path(handle.path), blocking=False))
            except BlockingIOError:
                return None
            path, scope, request, row = _permission_record(agent.conversation_store, agent.home_paths.root, receipt)
            if path != handle.path or not _record_matches(row, handle.scope, handle.request) or row.get('decision_input') != raw:
                raise ValueError('private approval waiter identity changed')
            prior = _decision_from_payload(row)
            if prior is not None:
                return prior
            if receipt.state != 'executing':
                return ToolApprovalDecision(request.permission_id, 'unavailable', reason_code='APPROVAL_UNAVAILABLE')
            return _apply_input_locked(agent, handle, receipt, scope, request)


# LLM: 持原op/permission且原scope/期限已由wait复核；当前密码/admin/MAC再次核验，失败原位unavailable，不等待解锁后应用旧点击。
# 函数用途: 复用唯一审批inner写决定和审计，不从JSON创建当前writer/claim/attempt。
def _apply_input_locked(agent, handle, receipt, scope, request):
    from ..common.json_io import read_json_object_report
    from ..common.json_io import write_json_file_atomic as write_row
    from ..conversation.agent_tool_approval import _resolve_agent_tool_approval_locked
    from .private_interaction_service import validated_private_interaction_from_config
    body = {key: receipt.private_approval[key] for key in ('reference', 'decision', 'message_id', 'chat_id')}
    try:
        payload, locked = validated_private_interaction_from_config(agent.home_paths.root, agent.config, body,
            channel=receipt.channel, actor_id=receipt.user_id)
    except ValueError:
        payload, locked = None, {'error_code': 'IM_INTERACTION_INVALID'}
    if locked is not None:
        decision = ToolApprovalDecision(request.permission_id, 'unavailable', reason_code='APPROVAL_UNAVAILABLE')
        row = read_json_object_report(handle.path, context='private.approval.retire').payload
        write_row(handle.path, {**row, 'status': 'decided', 'decision': decision.to_dict(),
                                'decision_input_error_code': str(locked['error_code'])}, blocking=False)
        return decision
    decision = ToolApprovalDecision(request.permission_id, body['decision'])
    _resolve_agent_tool_approval_locked(agent, handle.path, handle.scope, request, decision,
                                       interaction_actor=payload, blocking=False)
    return decision


# LLM: 原业务结果仅作同receipt安全投影；不携工具参数、私密表单或MAC，成功只认原决定器已提交结果。
# 函数用途: 将原批准/拒绝结果封到同一个控制终态。
def _decision_result(result):
    return ConversationControlResult(_KIND, bool(result['ok']),
        str(result.get('message') or ('已处理原审批决定。' if result['ok'] else '原审批决定未生效。')), details=result)


# LLM: queued/executing与unknown均不是批准；只重放原completed.details，不主动执行、重排、修账或清P3。
# 函数用途: 给原私卡回调返回准确受理状态，保留卡片直到执行者确认。
def private_approval_status(receipt):
    if receipt.state == 'completed':
        return dict(receipt.result['details'])
    if receipt.state == 'terminal_unknown':
        return {'ok': False, 'status': 'unknown', 'operation_id': receipt.operation_id,
                'decision': 'pending', 'error_code': 'CONTROL_OPERATION_DELIVERY_UNKNOWN',
                'message': '原审批点击的结果尚未确认；系统不会重复批准，请查询原任务状态。'}
    return {'ok': True, 'status': 'queued', 'operation_id': receipt.operation_id, 'decision': 'pending',
            'message': '审批点击已受理，仍待原执行者核验；尚未批准或拒绝本次调用。'}


# LLM: 只按真实异常类保持原对象，不以字符串码升级；此查询不授权、不证明未执行，也不释放P3或线程责任。
# 函数用途: 让原W控制catch在结算后仍上抛实际私卡运输未结异常。
def is_private_approval_transport_error(error):
    from ..backends.bounded_call import BoundedCallExitUnconfirmedError
    from ..common.request_execution_ports import (
        RequestExecutionContextUnavailable,
        RequestExecutionOutcomeUncertainError,
    )
    from ..common.tool_write_context import ToolWriteContextUnavailable
    from ..conversation.agent_tool_approval import ApprovalRetirementUnconfirmedError
    return isinstance(error, (RequestExecutionOutcomeUncertainError, RequestExecutionContextUnavailable,
                              BoundedCallExitUnconfirmedError, ToolWriteContextUnavailable, GlobalWriteError,
                              ApprovalRetirementUnconfirmedError))

# LLM: 只认真实类型，普通同码异常不升级；同一未结对象必须交原请求/P3出口，不当unavailable退休。
# 函数用途: 在旧best-effort边界优先穿透实际运输异常。
def raise_private_approval_transport_error(error):
    if is_private_approval_transport_error(error):
        raise error


# LLM: 新输入仍守原已发送私人卡及绝对截止；只读对账不调用本门，以免过期抹掉已记录决定。
# 函数用途: 在首次prepare或提交输入前拒绝错卡、未送达或过期的原审批。
def _require_delivered_approval(store, home_root, receipt):
    from ..conversation.im_tool_approval import im_approval_wait_live
    try:
        _path, _scope, _request, row = _permission_record(store, home_root, receipt)
    except FileNotFoundError as error:
        raise ValueError('private approval original card is unavailable') from error
    if not im_approval_wait_live(row, time.time()):
        raise ValueError('private approval original card is unavailable')


# LLM: 仍持原source/op锁；Count0原写，本机签卡固定raw叶；已决定只投影否则原未知，不补input，二次失败仍保最初异常对象。
# 函数用途: 避免本机输入部分失败永远伪装queued，同时保原部分事实与异常。
def _settle_input_failure_locked(agent, paths, receipt, error, *, blocking, source=None):
    try:
        updated = reconcile_private_approval_locked(paths, receipt, agent=agent)
        if updated.state != 'completed':
            updated = replace(receipt, state='terminal_unknown', updated_at=time.time(),
                error=runtime_error_report(error, context='private.approval.input'))
        if source is None:
            write_json_file_atomic(ops.gateway_control_operation_path(paths, receipt.operation_id), updated.to_dict(),
                                   **({} if blocking else {'blocking': False}))
        else:
            _write_original_local_receipt(source, paths, updated, blocking=blocking,
                **({'expected': receipt} if updated.state == 'completed' else {}))
    except BaseException:
        error.add_note('private_approval_settlement_unconfirmed')
        raise error from None


# LLM: 仅原签卡source.perform内的本机固定写口；source/current/完整raw窗口在原leaf末核，不新pin，不重投input；Count0不调用。
# 函数用途: 让原prepared/executing/失败收口及实际对账写进入同一原C完整回执观察。
def _write_original_local_receipt(source, paths, receipt, *, blocking, expected=None):
    from .multiprocess_local_control import (
        _commit_original_private_approval_reconciliation,
        _write_original_control_receipt,
    )
    if paths is not source.paths or blocking:
        raise ValueError('private approval original nonblocking writer required')
    if expected is not None:
        return _commit_original_private_approval_reconciliation(source, expected, receipt)
    return _write_original_control_receipt(source,
        ops.gateway_control_operation_path(paths, receipt.operation_id), receipt.to_dict())
