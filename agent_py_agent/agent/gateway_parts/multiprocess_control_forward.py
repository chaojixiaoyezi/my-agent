# LLM: 转发共用原控制回执；冷HTTP只发布完整raw CAS的prepared+dispatch，不物化owner；原ready/Count0入口及v5私卡保持。
# 模块用途: 为远端控制保存一次固定分区和代次，重试/状态只读原操作。
import time
from dataclasses import replace

from ..runtime_errors import DataCorruptionError
from ..user_space.owner_resolver import OwnerIdentity
from .io import write_json_file_atomic
from .multiprocess_coordinator import coordinator_for_root, coordinator_role_present
from .multiprocess_route import _digest, owner_digest, owner_worker


# LLM: 路径只由原operation_id校验器给出；读原字节/缺失事实，不加锁或修复坏内容，最终提交仍须原同路径try锁及完整字节CAS。
# 函数用途: 捕获冷运输的规范回执原件，不把不可读当空文件。
def read_control_operation_raw(paths, operation_id, *, deadline):
    from ..common.json_io import check_io_deadline
    from .control_operation_service import gateway_control_operation_path
    check_io_deadline(deadline)
    path = gateway_control_operation_path(paths, operation_id)
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        raw = None
    check_io_deadline(deadline)
    return raw


# LLM: 仅固定真实HTTP源持原op T调用；目标慢读在Root外，同门只try末核，prepared与dispatch一次原writer发布；已有raw任何变化只返回严格原事实。
# 函数用途: 给冷C首次远端控制完成原目标、分区和完整回执的原子提交。
def prepare_coordinator_remote_control(source, receipt, expected_raw):
    from ..common.json_io import (
        locked_json_path,
        write_private_json_file_atomic_no_newline_unlocked,
    )
    from . import control_operation_service as ops
    from .coordinator_control_transport import _CoordinatorControlTransport
    from .multiprocess_control_target import (
        TARGETED_CONTROL_KINDS,
        freeze_control_execution,
        refuse_unfreezable_control,
    )
    from .multiprocess_global_write import GlobalWriteError

    if type(source) is not _CoordinatorControlTransport:
        raise GlobalWriteError('control_http_source_required', 'control_transport')
    source.require_current()
    original = ops._new_control_operation_receipt(source.scope, source.command.kind, source.command_text,
        source.operation_id, source.input_digest)
    if replace(original, updated_at=0) != replace(receipt, updated_at=0) or expected_raw is not None:
        raise GlobalWriteError('control_receipt_source_changed', 'control_transport')
    target_options = {'config': source.control.config, 'home_root': source.control.home_root,
                      'blocking': False, 'deadline': source.deadline}
    targeted = receipt.command_kind in TARGETED_CONTROL_KINDS
    execution = freeze_control_execution(None, source.paths, receipt, **target_options) if targeted else None
    route = _control_dispatch_for_receipt(receipt, source.lease, source.lease.c_run_id, execution)
    forwarded = (refuse_unfreezable_control(receipt, time.time()) if targeted and execution is None
                 else replace(receipt, dispatch=route))
    if forwarded.dispatch is not None:
        validate_control_dispatch(forwarded)
    results = []

    # LLM: 原controller在mutex/lease短门同步调用，Root内无领域/网络/等待；JSON只try、末writer用同一个deadline，失败不补发。
    # 函数用途: 最后复核原HTTP来源、目标和完整原件，再一次发布规范回执。
    def commit():
        with source.runtime._gate:
            source.require_current()
            path = ops.gateway_control_operation_path(source.paths, source.operation_id)
            with locked_json_path(path, blocking=False):
                source.require_current()
                current = read_control_operation_raw(source.paths, source.operation_id, deadline=source.deadline)
                if current != expected_raw:
                    existing = ops._coordinator_control_receipt(source, current)
                    if existing is None:
                        raise GlobalWriteError('control_receipt_changed', 'control_transport')
                    results.append(existing)
                    return
                if targeted and freeze_control_execution(None, source.paths, receipt, **target_options) != execution:
                    raise GlobalWriteError('control_execution_target_changed', 'control_transport')
                source.require_current()
                write_private_json_file_atomic_no_newline_unlocked(path, forwarded.to_dict(),
                    sort_keys=True, deadline=source.deadline)
                results.append(forwarded)

    decision = source.controller.launch_owner_writer(owner_digest(source.owner), commit)
    if decision.get('status') != 'started' or len(results) != 1:
        raise GlobalWriteError('control_owner_unavailable', 'control_transport')
    return results[0]


# LLM: ready与cold只在这里组装同一分区/代次/目标binding；不读取文件、不决定准入或签执行权。
# 函数用途: 给原两种受理形态复用唯一控制路由公式。
def _control_dispatch_for_receipt(receipt, lease, generation, execution):
    from .control_operation_service import _receipt_owner_identity
    route = {'worker': owner_worker(_receipt_owner_identity(receipt), lease.count),
             'count': lease.count, 'c_run_id': generation}
    if execution is not None:
        route['execution'] = execution
    route['binding'] = control_dispatch_binding(receipt, route)
    return route


# LLM: 输入是已校验原回执；排除可变执行状态，绑定全部冻结身份/命令/权限/目录/目标及路由。
# 函数用途: 防止转发字段或原回执被重试改成另一个任务。
def control_dispatch_binding(receipt, dispatch):
    payload = receipt.to_dict()
    for key in ("dispatch", "state", "result", "error", "updated_at", "conversation_thread_id"):
        payload.pop(key, None)
    frozen_route = {key: dispatch[key] for key in ("worker", "count", "c_run_id")}
    if 'execution' in dispatch:
        frozen_route['execution'] = dispatch['execution']
    return _digest({"receipt": payload, "route": frozen_route})


# LLM: 纯校验不读配置/owner；分区与完整冻结输入一致，认领必须有P0完整出生及实例。
# 函数用途: 读取转发回执时拒绝自洽错分区或目标篡改。
def validate_control_dispatch(receipt):
    from .control_operation_service import _receipt_owner_identity

    route = receipt.dispatch
    if not isinstance(route, dict):
        raise DataCorruptionError("control dispatch is invalid")
    count, worker = route.get("count"), route.get("worker")
    owner = _receipt_owner_identity(receipt)
    if (type(count) is not int or count <= 0 or type(worker) is not int or not 0 <= worker < count
            or owner == OwnerIdentity.local_main() or owner_worker(owner, count) != worker
            or type(route.get("c_run_id")) is not str or not route["c_run_id"].strip()
            or route.get("binding") != control_dispatch_binding(receipt, route)):
        raise DataCorruptionError("control dispatch binding is invalid")
    from .execution_target import GatewayExecutionTarget
    from .multiprocess_control_target import TARGETED_CONTROL_KINDS

    if receipt.command_kind in TARGETED_CONTROL_KINDS:
        target = GatewayExecutionTarget.from_dict(route.get('execution'))
        if (target is None
                or (receipt.input_digest_version >= 4 and target.to_dict() != receipt.expected_execution)
                or (receipt.expected_turn_id and target.turn_id != receipt.expected_turn_id)):
            raise DataCorruptionError('control dispatch execution target is invalid')
    _validate_control_executor(receipt, route)


# LLM: 复用P0身份schema，不接受PID-only、布尔PID或缺实例；prepared不能伪装成旧认领。
# 函数用途: 校验控制认领附加字段与其状态一致。
def _validate_control_executor(receipt, route):
    from .multiprocess_records import MultiprocessRecordError, validate_record

    if receipt.state in {'claimed', 'executing', 'completed'} or 'executor' in route:
        try:
            identity = validate_record(route.get('executor'))
        except MultiprocessRecordError as error:
            raise DataCorruptionError('control executor identity is invalid') from error
        if (identity['schema_version'] != 'multiprocess_process_identity.v1'
                or type(route.get('instance_id')) is not str or not route['instance_id'].strip()):
            raise DataCorruptionError('control executor instance is invalid')
    if receipt.state == 'prepared' and ('executor' in route or 'instance_id' in route):
        raise DataCorruptionError('prepared control has executor')


# LLM: 原操作锁内固定路由，组装公式与cold共纯叶；C管理kind保实际owner走冷管理，不转W/live issuer；原lease/代次/阻塞默认不变。
# 函数用途: C首次远端控制只入队；私卡显式传原control引用，默认原选择/阻塞锁不变，私卡CFalse沿原同锁，旧上下文不借新C提交。
def prepare_remote_control(agent, paths, receipt, *, fresh=False, control_context=None, blocking=True):
    from .control_operation_service import _receipt_owner_identity, gateway_control_operation_path

    if receipt.dispatch is not None:
        return receipt
    context = _coordinator_context_paths(agent, paths) if control_context is None else control_context
    lease = coordinator_for_root(paths.root, context=context)
    if lease is None and coordinator_role_present(paths.root, context=context):
        # 拒新入不代表C变成单进程业务执行者；保留失败关闭，不能返回同步回退信号。
        raise DataCorruptionError("control coordinator generation is unavailable")
    from .owner_management_control import is_owner_management_control
    if (lease is None or _receipt_owner_identity(receipt) == OwnerIdentity.local_main()
            or is_owner_management_control(receipt.command_kind)):
        return None
    # 旧终态重放也不能进入owner回执对账。
    if receipt.state != "prepared":
        return receipt
    from .multiprocess_control_target import (
        TARGETED_CONTROL_KINDS,
        freeze_control_execution,
        refuse_unfreezable_control,
    )

    generation = lease.c_run_id
    if not generation:
        raise DataCorruptionError('control coordinator generation is unavailable')
    # 原操作锁冻结本次输入；目标随后改变由W原严格比较拒绝，不在这里重新选目标。
    execution = (freeze_control_execution(agent, paths, receipt)
                 if fresh and receipt.command_kind in TARGETED_CONTROL_KINDS else None)
    with lease._lifecycle_gate:
        if coordinator_for_root(paths.root, context=context) is not lease or lease.c_run_id != generation:
            raise DataCorruptionError('control coordinator generation is unavailable')
        route = _control_dispatch_for_receipt(receipt, lease, generation, execution)
        if receipt.command_kind in TARGETED_CONTROL_KINDS:
            if execution is None:
                refused = refuse_unfreezable_control(receipt, time.time())
                write_json_file_atomic(gateway_control_operation_path(paths, receipt.operation_id), refused.to_dict(), **({} if blocking else {"blocking": False}))
                return refused
        forwarded = replace(receipt, dispatch=route)
        validate_control_dispatch(forwarded)
        write_json_file_atomic(gateway_control_operation_path(paths, receipt.operation_id), forwarded.to_dict(), **({} if blocking else {"blocking": False}))
        return forwarded


# LLM: 仅取宿主组合根已经绑定的原paths引用；不读配置/JSON，也不形成新的授权源。
# 函数用途: 为晚到HTTP调用保留原C的限制性上下文。
def _coordinator_context_paths(agent, paths):
    control = getattr(agent, '_gateway_control_plane', None)
    return control if control is not None else paths


# LLM: 不进入远端owner；原C角色即使撤权仍不变，live/unknown只读，不重放业务。
# 函数用途: 查询原控制回执，防止C进入插话对账或再执行死亡认领。
def read_remote_control_status(paths, receipt, *, agent=None):
    from .control_operation_service import _receipt_owner_identity

    context = _coordinator_context_paths(agent, paths)
    if receipt.dispatch is not None:
        validate_control_dispatch(receipt)
        return _seal_dead_control(paths, receipt, context=context)
    if coordinator_role_present(paths.root, context=context) and _receipt_owner_identity(receipt) != OwnerIdentity.local_main():
        return receipt
    return None


# LLM: 原op锁→lease内核死亡/代次；私卡先沿原C资源读决定事实，不能将已决定洗unknown，仍不执行决定或物化Agent。
# 函数用途: 同域明确死亡的控制可封unknown，旧HTTP上下文只读并保留规范字节。
def _seal_dead_control(paths, receipt, *, context=None):
    from . import control_operation_service as ops
    from .multiprocess_control_identity import control_executor_live

    lease = coordinator_for_root(paths.root, context=context)
    if (lease is None or receipt.state not in {'claimed', 'executing'}
            or control_executor_live(receipt.dispatch.get('executor')) is not False):
        return receipt
    with ops.try_gateway_control_operation_transition(paths, receipt.operation_id) as acquired:
        if not acquired:
            return receipt
        current = ops.read_gateway_control_operation(paths, receipt.operation_id)
        if current != receipt or control_executor_live(current.dispatch.get('executor')) is not False:
            return current
        with lease._lifecycle_gate:
            if coordinator_for_root(paths.root, context=context) is not lease:
                return current
            if current.private_approval is not None:
                from .private_approval_control import reconcile_private_approval_locked
                observed = reconcile_private_approval_locked(paths, current, control=lease.control_plane)
                if observed.state == 'completed':
                    write_json_file_atomic(ops.gateway_control_operation_path(paths, current.operation_id), observed.to_dict())
                    return observed
            unknown = replace(current, state='terminal_unknown', updated_at=time.time(),
                              error={'error_code': 'CONTROL_EXECUTOR_DEAD',
                                     'message': 'prior executor is dead; control effect will not be replayed'})
            write_json_file_atomic(ops.gateway_control_operation_path(paths, current.operation_id), unknown.to_dict())
            return unknown
