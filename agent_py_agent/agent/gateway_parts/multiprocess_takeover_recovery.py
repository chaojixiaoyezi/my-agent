# LLM: 只在原C租约内运行只读P2复核，不调P3/启动写者；首次实际Thread在start前保原引用/ACK未知，P6在原业务结清后停止并门外join，未知不从空表/is_aliveFalse结清，同步原启动/关闭与P6测试。
# 模块用途: 串行观察隔离旧域，保留本代原复核线程给实际停止消费者。
import threading
import time
from functools import wraps

# 前台保留原五次/一秒；后台提高尝试上限和接受期限，不抹失败或硬中断系统调用。
FOREGROUND_BUDGET = {"schema_version": "multiprocess_takeover_budget.v1", "max_attempts": 5, "deadline_seconds": 1.0}
RECHECK_BUDGET = {"schema_version": "multiprocess_takeover_budget.v1", "max_attempts": 40, "deadline_seconds": 5.0}
# 关闭通知竞态的只读保底检查间隔；不重跑P2或扫描文件，正常恢复仍即时通知。
OWNER_RECOVERY_STOP_CHECK_SECONDS = 1.0


# LLM: 具名有限方法保原接口；新增owner物化门只用于C建立HTTP后的真实factory，补章仍不持内存门等T。
# 函数用途: 选择保留原字段/可重入内存锁的有限适配器，不补C租约或运输授权。
def serialized_takeover(callback):
    factories = {"prepare_target": _guard_prepare, "restart_worker": _guard_restart,
                 "launch_worker": _guard_launch, "restamp_request": _guard_restamp,
                 "authority_rejection": _guard_authority, "admission_rejection": _guard_admission,
                 "launch_owner_writer": _guard_owner_launch}
    if callback.__name__ not in factories:
        raise ValueError("takeover_method_not_reviewed")
    return factories[callback.__name__](callback)


# LLM: 保留target与budget具名协议，不能把预算参数吞成泛化kwargs。
# 函数用途: 串行一次目标准备，允许原调用的budget关键字。
def _guard_prepare(callback):
    # LLM: 原callback仍检查真实租约和回执；适配器只持可重入内存门。
    # 函数用途: 按原具名接口调用目标核实。
    @wraps(callback)
    def guarded(controller, target, budget=None):
        with controller._mutex:
            return callback(controller, target, budget)
    return guarded


# LLM: 重启副作用仍在原callback的当次核实后，不搬运或缓存launch权限。
# 函数用途: 保留target/launch接口的串行重启适配器。
def _guard_restart(callback):
    # LLM: 锁内同步调用，launch不跨原核实窗口。
    # 函数用途: 串行原重启门。
    @wraps(callback)
    def guarded(controller, target, launch):
        with controller._mutex:
            return callback(controller, target, launch)
    return guarded


# LLM: 分区字段显式绑定，不以任意参数扩展新写者入口。
# 函数用途: 保留index/launch接口的串行分区门。
def _guard_launch(callback):
    # LLM: 仍由原callback判断完整旧域和最后源检查。
    # 函数用途: 串行原分区启动门。
    @wraps(callback)
    def guarded(controller, index, launch):
        with controller._mutex:
            return callback(controller, index, launch)
    return guarded


# LLM: 只适配已审查的owner_sha/launch接口，真实许可和撤权门仍在原callback，不运输或缓存启动权限。
# 函数用途: 串行控制面建立后的基础业务物化决定。
def _guard_owner_launch(callback):
    # LLM: 保持精确参数，在可重入P2门内消费原隔离决定。
    # 函数用途: 同步调用本代owner业务factory门。
    @wraps(callback)
    def guarded(controller, owner_sha, launch):
        with controller._mutex:
            return callback(controller, owner_sha, launch)
    return guarded


# LLM: P3具名字段/原同步窗口不变；callback准备自己串行，消费方T→内存，不形成内存→T反向边。
# 函数用途: 保留补章接口，不在等待请求T锁时占住后台内存门。
def _guard_restamp(callback):
    # LLM: callback只在准备/读回段持内存门，实际P3门内统一锁序；后台不能获得同步授权。
    # 函数用途: 在原同步调用中完成一次补章，不运输许可。
    @wraps(callback)
    def guarded(controller, gate, path, ownership):
        return callback(controller, gate, path, ownership)
    return guarded


# LLM: 权威仍由原对象/字节/原同步调用决定，不接受泛化额外字段。
# 函数用途: 保留gate/authority接口的串行复核。
def _guard_authority(callback):
    # LLM: 不修改原P3锁内源与撤权检查。
    # 函数用途: 串行原临时授权核对。
    @wraps(callback)
    def guarded(controller, gate, authority):
        with controller._mutex:
            return callback(controller, gate, authority)
    return guarded


# LLM: owner只读拒绝仍是原精确字段，不以适配器赋予准入权。
# 函数用途: 保留owner_sha接口的串行隔离查询。
def _guard_admission(callback):
    # LLM: Condition持同一RLock时此查询可安全重入。
    # 函数用途: 串行原owner隔离读取。
    @wraps(callback)
    def guarded(controller, owner_sha):
        with controller._mutex:
            return callback(controller, owner_sha)
    return guarded


# LLM: 入口仅同进程准确当前C/controller；start前保存首次Thread及未知ACK，成功才清未知；线程不执行用户正文或写者，不得由W调用，P6消费准确原对象。
# 函数用途: 在出现可定位隔离范围时启动唯一后台复核线程。
def start_takeover_rechecks(controller):
    from .multiprocess_takeover_runtime import CoordinatorTakeover
    if type(controller) is not CoordinatorTakeover:
        raise ValueError('original_coordinator_recheck_required')
    if not controller.active() or not controller._rechecks or controller._recheck_thread is not None:
        return
    thread = threading.Thread(target=_run_rechecks, args=(controller,), name="c-takeover-recheck", daemon=True)
    controller._recheck_thread = thread
    controller._recheck_thread_origin = thread
    controller._recheck_thread_start_unknown = True
    thread.start()
    controller._recheck_thread_start_unknown = False


# LLM: 只消费调度时刻，不采用持久证明；单次失败保守收尾与循环拆开，减少锁/异常分支耦合。
# 函数用途: 用Event等待下一到期点或收尾，不忙轮询、不发信号。
def _run_rechecks(controller):
    while not controller._recheck_stop.wait(_next_delay(controller)):
        if not controller.active() or controller._all_quarantined:
            return
        if not _recheck_once(controller):
            return


# LLM: 沿原只读P2复核，未知异常只能保守整域隔离并退出，不产生写者/P3许可或吞控制者中断。
# 函数用途: 执行一次到期复核；成功继续，意外错误标隔离并让循环收尾。
def _recheck_once(controller):
    try:
        controller.recheck_due()
    except Exception:
        with controller._mutex:
            controller._all_quarantined = True
        return False
    return True


# LLM: 调度时刻只在同C锁内读，长观察可能延迟下一次检查；无hard wall-time承诺。
# 函数用途: 算有下限的Event等待时间，不改退避状态。
def _next_delay(controller):
    with controller._mutex:
        times = [state["next_check_at"] for _target, state in controller._rechecks.values()]
    return max(0.01, min(times) - time.monotonic()) if times else 1.0


# LLM: 不等待长观察所持内存门；正常P6先完成业务/P3才通知本原Thread，撤租约路径仍先撤原FD；门外join仅供观察，未知ACK/换ref由P6拒，不能从本bool签完成。
# 函数用途: 通知并有界等待原只读复核线程，预算结束仍如实保留活线程。
def stop_takeover_rechecks(controller):
    controller._recheck_stop.set()
    if controller._mutex.acquire(blocking=False):
        try:
            controller._condition.notify_all()
        finally:
            controller._mutex.release()
    thread = controller._recheck_thread
    if thread is not None and thread is not threading.current_thread():
        thread.join(timeout=RECHECK_BUDGET["deadline_seconds"] + 1.0)
    controller._recheck_joined = thread is None or not thread.is_alive()
    return controller._recheck_joined


# LLM: 持Condition只读原租约/隔离；到期后重获锁或初始已恢复都不接受迟到成功，不承诺硬抢占。
# 函数用途: 等后台通知或期限，缺范围/结构失败/租约撤销立即拒绝。
def wait_owner_recovery(controller, owner_sha, deadline) -> bool:
    while controller.active() and controller.admission_rejection(owner_sha):
        remaining = deadline - time.monotonic()
        if controller._all_quarantined or not controller._rechecks or remaining <= 0:
            return False
        controller._condition.wait(timeout=min(remaining, OWNER_RECOVERY_STOP_CHECK_SECONDS))
    return (controller.active() and controller.admission_rejection(owner_sha) is None
            and time.monotonic() < deadline)
