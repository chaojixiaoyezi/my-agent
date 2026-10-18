# LLM: 精确原W来源或原CoordinatorExecutorSource各消费同原issuer/lease/T/P3，角色由实际C对象决定；不造W issuer/新authority，不从wire/JSON授信，同步host/P5/C scope tests。
# 模块用途: 在原native host协议内区分C主体和W来源的原pin、原准入及原生命周期锁。
from .io import read_json_file_report
from .multiprocess_executor_original_source import OriginalCExecutorSource
from .multiprocess_native_short_operation import require_original_native_admission
from .multiprocess_records import record_digest
from .multiprocess_worker_commit import WorkerCommitIssuer, WorkerCommitRejected


# LLM: 只消费原C内精确类，import C仅非W分支；纯角色判定不做I/O/授权，实际短效果仍原source._commit。
# 函数用途: 区分真实本地C主体来源，拒绝壳对象及角色文本。
def coordinator_host_source(source):
    if type(source) is OriginalCExecutorSource:
        return False
    from .multiprocess_coordinator_executor_source import CoordinatorExecutorSource
    if type(source) is not CoordinatorExecutorSource:
        raise WorkerCommitRejected('original_native_host_authority_required')
    source._require_source_identity()
    return True


# LLM: 新pin及受信记账callback在同原T/P3/C门，callback不经wire；C API原lease承接责任，W原issuer.begin保原peer。
# 函数用途: 在实际创建之前原子保住host或命令的原责任引用。
def begin_host_pin(source, on_pin, source_guard=None):
    if coordinator_host_source(source):
        return source.begin_execution_domain(source_guard=source_guard, on_pin=on_pin)

    # LLM: 原W source._commit内由同一issuer建立pin并同步交原on_pin，callback失败不能冒称无责任或另补许可。
    # 函数用途: 在实际创建前保留原W执行责任，不启动进程或发信号。
    def remember():
        pin = source.issuer.begin(source.owner, peer_pid=source._sources[6].pid)
        on_pin(pin)
        return pin
    return source._commit(remember, source_guard=source_guard)


# LLM: closing仍可精确结清原责任，但pin绝不授新执行；C/W holder由原session保存不是消息选择。
# 函数用途: 通过原实际来源归还自己的精确pin。
def finish_host_pin(holder, pin):
    if type(holder) is WorkerCommitIssuer:
        holder.finish(pin)
    elif coordinator_host_source(holder):
        holder.finish_execution_domain(pin)


# LLM: 只能在原source._commit持有原T/C时调用；C当次claim/P3门已由原transition核，当前规范P3、epoch和源码自产绑定仍须一致。
# 函数用途: 固定原host准入摘要，C不会假装W路由或引入另一名额。
def host_admission(source, row):
    if not coordinator_host_source(source):
        p3 = require_original_native_admission(source.issuer, source.owner, source._transition)
    else:
        from . import multiprocess_admission as admission
        source._bound_record(row)
        p3 = admission._read_reservation(admission.reservation_file(source._sources[1].paths, source._transition.request_id))
        if (p3 is None or p3['state'] != 'occupied' or p3['request_id'] != row['request_id']
                or p3['owner_sha'] != row['owner_sha']
                or source._sources[4]._accepted.get(row['request_id']) != record_digest(p3)):
            raise WorkerCommitRejected('original_native_host_admission_required')
    payload = read_json_file_report(source._transition.request_path, context='native.host.binding').payload
    if (p3['route'] != payload.get('route') or payload.get('lease_epoch') != row['lease_epoch']
            or payload.get('execution_attempt_id') != row['execution_attempt_id']):
        raise WorkerCommitRejected('original_native_host_binding_required')
    return p3


# LLM: 原C源与W源各持实际原lease，锁只是同步不能授信；角色不来自JSON或分区号。
# 函数用途: 返回原来源用于清理序列化的租约生命周期锁。
def host_lifecycle_gate(source):
    source._require_source_identity()
    return source._sources[5 if coordinator_host_source(source) else 4]._lifecycle_gate


# LLM: C holder就是原C source，W holder就是原issuer；同一对象表随原session保留，不另建authorityfacade。
# 函数用途: 保存责任表实际需要结清的原来源。
def host_pin_holder(source):
    return source if coordinator_host_source(source) else source.issuer
