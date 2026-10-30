# LLM: 完整原范围直接调P2；缺旧N责任仅原调度只读复核，未知永不解除，P3仍只原同步授权窗口；同步partition及recheck来源。
# 模块用途: 沿原租约核旧范围/fresh P2，持有v2关闭原字节及固定回执raw增量；增量不刷新P1/P3基线，未知不授执行权。
import copy
import json
import logging
import threading
import time
from contextvars import ContextVar

from . import multiprocess_takeover as p2
from .multiprocess_coordinator import bind_coordinator_generation, current_coordinator
from .multiprocess_route import owner_digest
from .multiprocess_takeover_recheck import _fresh_verdict, new_quarantine_state, quarantine_recheck
from .multiprocess_takeover_recovery import (
    FOREGROUND_BUDGET,
    RECHECK_BUDGET,
    serialized_takeover,
    start_takeover_rechecks,
    stop_takeover_rechecks,
)
from .multiprocess_takeover_sources import (
    TakeoverTarget,
    confirmed_evidence,
    discover_takeover_targets,
    require_role_coverage,
    takeover_namespace_state,
    uncovered_admission_owners,
)
from .multiprocess_takeover_unresolved_source import (
    UnresolvedExecutorSource,
    unresolved_recheck_state,
)


# LLM: 控制器仅附真实锁并保存旧关闭raw；后台无P3权，历史关闭范围不替fresh P2，不靠TTL/心跳/覆盖state解除未知。
# 类用途: 统一当前C启动/重启/改代及只读复核，隔离/调度不持久化第二账本。
class CoordinatorTakeover:
    # LLM: 原guard登记lease；mutex保存原目标/限制与捕获关闭raw，未知不造范围，关闭历史不授P2/P3且后台不继承授权。
    # 函数用途: 保存启动来源及原租约内隔离/调度事实，不持久化证明。
    def __init__(self, paths, count, c_run_id):
        self.paths, self.count, self.c_run_id = paths, count, c_run_id
        self._lease = current_coordinator(paths.root, count)
        self.targets = []
        self._unresolved_sources = ()
        self._quarantined = set()
        self._unproven_owners = set()
        self._all_quarantined = False
        self._issued = ContextVar("coordinator_takeover_issued", default=None)
        self._namespaces = None
        self._accepted = {}
        self._mutex = threading.RLock()
        self._condition = threading.Condition(self._mutex)
        self._bootstrap_sources = None
        self._closed_role_sources = ()
        # 仅真实当前接班捕获；磁盘原像不能重建此活对象或原lease绑定。
        self._standalone_migration = None
        self._standalone_origin = None
        # 只保存原C已捕获的Popen/issuer活对象；不是从P1或P4文件重建的权限账。
        self._current_workers = {}
        # 首绑定原引用独立捕获；issuer公开字段的替换不能改变原进程或责任作用域。
        self._current_worker_origins = {}
        # 本地原C认领的自产P1来源；只持原对象，不从JSON恢复权限或作为第二账本。
        self._current_coordinator_sources = []
        self._current_coordinator_native_host_roots = []
        self._rechecks = {}
        self._inflight = set()
        self._recheck_stop = threading.Event()
        self._recheck_thread = None
        self._recheck_thread_origin = None
        self._recheck_thread_start_unknown = False
        self._recheck_joined = True

    # LLM: 只有相同租约/本进程/当前代和控制器仍挂在原租约上才有效；退出后对象引用不能延寿。
    # 函数用途: 检查当前实际持锁C是否仍拥有本接线对象。
    def active(self) -> bool:
        lease = current_coordinator(self.paths.root, self.count)
        return (lease is not None and lease is self._lease and lease.controller is self
                and lease.c_run_id == self.c_run_id)

    # LLM: 启动短预算，其它同步门可长观察，但每次仍核原回执/本次时窗/P1，拒绝复制和磁盘证明。
    # 函数用途: 按显式观察预算核完整旧域；未知排队，不启动写者。
    @serialized_takeover
    def prepare_target(self, target, budget=None):
        if not self.active() or target.ledger.root != self.paths.root or target.ledger.partition.count != self.count:
            return self._quarantine(target, "coordinator_context_unavailable")
        target = TakeoverTarget(target.ledger, copy.deepcopy(target.scope))
        result = None
        try:
            started = time.monotonic()
            result = p2.verify_takeover(target.ledger, target.scope, budget=dict(budget or FOREGROUND_BUDGET))
            if not _current_receipt(self, target, result, (started, time.monotonic())):
                raise ValueError("takeover_receipt_not_current")
            confirmed_evidence(target, result)
            _mark_target_stopped(self, target)
        except (OSError, ValueError, TypeError, KeyError):
            return self._quarantine(target, "execution_tree_not_stopped", result)
        return result

    # LLM: 每次重启需当次核实；最终回调与实际撤权共门，观察/源读回不占生命周期门，回调不可跨撤权。
    # 函数用途: 经完整核实后调用受信组合根的启动回调；失败不调用回调。
    @serialized_takeover
    def restart_worker(self, target, launch):
        if not _refresh_targets(self):
            return self._quarantine(target, "takeover_scope_unconfirmed")
        frozen = _source_snapshot(self)
        result = self.prepare_target(target, RECHECK_BUDGET)
        if result["error"] is not None:
            return result
        index = target.scope["worker_index"]
        failure = _verify_worker_partition(self, index, target.scope)
        if failure is not None:
            return failure
        if (any(self.admission_rejection(owner) for owner in target.scope["owner_shas"])
                or (index is not None and _partition_blocked(self, index)) or not _sources_unchanged(self, frozen)):
            return self._quarantine(target, "owner_scope_still_quarantined")
        if not _launch_current(self, launch):
            return self._quarantine(target, "coordinator_context_unavailable")
        return result

    # LLM: 每次重新核桶内全部旧域；最终启动与撤权共门，无旧账不产屏障，不接P4/P5请求执行。
    # 函数用途: 经当前C和完整旧域核实后启动一个空闲worker；未知绝不先启动。
    @serialized_takeover
    def launch_worker(self, index, launch):
        if not self.active() or type(index) is not int or not 0 <= index < self.count:
            return _blocked("coordinator_context_unavailable")
        if not _refresh_targets(self):
            return _blocked("takeover_scope_unconfirmed")
        frozen = _source_snapshot(self)
        failure = _verify_worker_partition(self, index)
        if failure is not None:
            return _blocked(failure["error"]["reason"])
        if _partition_blocked(self, index) or not _sources_unchanged(self, frozen):
            return _blocked("owner_partition_quarantined")
        if not _launch_current(self, launch):
            return _blocked("coordinator_context_unavailable")
        return {"status": "started", "worker_index": index, "c_run_id": self.c_run_id}

    # LLM: 只供受信组合根物化基础业务资源；消费原隔离决定/启动源快照，并与实际撤权共生命周期门。
    # 函数用途: C已建立HTTP后，只为当前未隔离owner调用一次真实业务factory；不接P5派发。
    @serialized_takeover
    def launch_owner_writer(self, owner_sha, launch):
        if (self.admission_rejection(owner_sha) is not None
                or not _sources_unchanged(self, self._bootstrap_sources)):
            return _blocked("owner_partition_quarantined")
        if not _launch_current(self, launch):
            return _blocked("coordinator_context_unavailable")
        return {"status": "started", "c_run_id": self.c_run_id}

    # LLM: 准备/等待T仍分段；P3消费门在同一T/内存决策区登记精确提交，不留解锁后的豁免窗口。
    # 函数用途: 为一份请求做当次核实后重新盖章，保留原预留/占用；隔离不改请求或预留。
    @serialized_takeover
    def restamp_request(self, gate, path, ownership):
        return _perform_request_restamp(self, gate, path, ownership)

    # LLM: 只在当次实际P2签发与原P3/T/租约末门内消费同步元数据回调，不返回可保存授权或授予模型/工具权。
    # 函数用途: 原旧执行者停止已核准后，在同一原短窗内恢复该processing记录。
    def recover_processing(self, gate, path, ownership, operation, after_operation=None):
        return _perform_request_restamp(self, gate, path, ownership, operation=operation, after_operation=after_operation)

    # LLM: 仅当前同步调用持有原对象/字节；锁内对照当次不可变覆盖快照，别的调用刷新共享缓存不能洗掉变化。
    # 函数用途: 在P3请求锁内复核临时授权、签发源快照、owner隔离与当前P1明细。
    @serialized_takeover
    def authority_rejection(self, gate, authority) -> str:
        issued = self._issued.get()
        if (not self.active() or gate.c_run_id != self.c_run_id or gate.paths.root != self.paths.root
                or gate.count != self.count or issued is None or issued[0] is not authority
                or not issued[4][0] or issued[4][1] != threading.get_ident()):
            return "takeover_authority_context_mismatch"
        if issued[1] != _authority_bytes(authority):
            return "takeover_barrier_invalid"
        if (not _sources_unchanged(self, issued[5])
                or any(self.admission_rejection(owner) for owner in issued[2].scope["owner_shas"])):
            return "takeover_barrier_invalid"
        try:
            confirmed_evidence(issued[2], issued[3])
        except (OSError, ValueError, TypeError, KeyError):
            self._quarantine(issued[2], "takeover_evidence_source_changed")
            return "takeover_barrier_invalid"
        return ""

    # LLM: 活着/未知旧域仍挡owner的新请求；C断锁后全部拒绝，不能因为心跳过期或线程返回而放行。
    # 函数用途: 返回owner准入拒绝或None，不修改持久预留。
    @serialized_takeover
    def admission_rejection(self, owner_sha):
        if (not self.active() or self._all_quarantined or owner_sha in self._unproven_owners
                or any(key[-1] == owner_sha for key in self._quarantined)):
            return _blocked("owner_partition_quarantined")
        return None

    # LLM: 隔离不是授权；保存规范旧scope的退避状态，缺范围整体隔离，不从文件复活证明。
    # 函数用途: 记录隔离、启动当前C只读复核并返回拒绝。
    def _quarantine(self, target, reason, result=None):
        keys = _scope_keys(target.scope)
        self._quarantined.update(keys)
        self._all_quarantined = self._all_quarantined or not keys
        _queue_target_recheck(self, target)
        value = dict(result) if isinstance(result, dict) else {"barrier": None, "proof": None}
        value["error"] = {"error_code": "OWNER_PARTITION_QUARANTINED", "reason": reason}
        return value

    # LLM: 长观察不占前台内存门；每项重新冻结/核来源，原state被替换或撤权则丢弃迟到回执。
    # 函数用途: 复核到期旧域并逐项通知，不改代/释放请求/启动写者。
    def recheck_due(self):
        with self._mutex:
            if not self.active() or self._recheck_stop.is_set():
                return []
            jobs = [(key, target, state) for key, (target, state) in self._rechecks.items()
                    if key not in self._inflight and time.monotonic() >= state["next_check_at"]]
        return [_recheck_target(self, key, target, state) for key, target, state in jobs]

    # LLM: 只读等后台结果；争门及最终来源读回共用同一接受截止，迟到成功也拒绝，不承诺硬中断I/O。
    # 函数用途: 在创建Agent前有界等待owner解除，再核启动源未被晚到范围洗掉。
    def wait_for_owner(self, owner_sha, seconds=8.0) -> bool:
        from .multiprocess_takeover_recovery import wait_owner_recovery

        deadline = time.monotonic() + max(0.0, float(seconds))
        if not self._mutex.acquire(timeout=max(0.0, deadline - time.monotonic())):
            return False
        try:
            ready = wait_owner_recovery(self, owner_sha, deadline)
            return (ready and _sources_unchanged(self, self._bootstrap_sources)
                    and self.admission_rejection(owner_sha) is None and time.monotonic() < deadline)
        finally:
            self._mutex.release()

    # LLM: 只由原C租约收尾调用；先撤销lease，迟到回执不得解除；不向旧树发信号。
    # 函数用途: 收尾自己的后台线程，保持P1/P3原记录不变。
    def close(self):
        return stop_takeover_rechecks(self)


# LLM: 调用方持内存门；源核实在门外完成，最终回调与真实撤权互斥，阻塞回调不能被硬抢占。
# 函数用途: 仅原租约仍活动且未收尾时调用启动副作用，不向旧树发信号。
def _launch_current(controller, launch) -> bool:
    with controller._lease._lifecycle_gate:
        if not controller.active() or controller._recheck_stop.is_set():
            return False
        launch()
        return True


# LLM: 原P2长观察在T外；hot与T外post仍同一次issued同步pipeline，每leaf独立末门；finally撤销全部继承值，不返回可保存closure。
# 函数用途: 为补章或恢复提供同一当次真实P2签发区间，不建立第二授权账。
def _perform_request_restamp(controller, gate, path, ownership, *, operation=None, after_operation=None):
    failure, prepared = _prepare_request_authority(controller, gate, path, ownership, recovery=operation is not None)
    if failure is not None:
        return failure
    authority, target, result, frozen = prepared
    live = [True, threading.get_ident()]
    token = controller._issued.set((authority, _authority_bytes(authority), target, result, live, frozen))
    try:
        if operation is None:
            return gate.restamp_for_takeover(path, ownership, authority)
        decision = gate.restamp_for_takeover(path, ownership, authority, operation=operation)
        if after_operation is not None and not isinstance(decision, dict):
            return after_operation(decision, authority)
        return decision
    finally:
        live[0] = False
        controller._issued.reset(token)


# LLM: 准备授权只持内存门、不争T；恢复从原P1 claim定位完整旧域，普通补章仍读P3 route；返回当次原回执，T内仍核来源/租约。
# 函数用途: 完成本次请求的同步核实后准备临时授权，失败不调用P3。
def _prepare_request_authority(controller, gate, path, ownership, *, recovery=False):
    with controller._mutex:
        if not _refresh_targets(controller):
            return _blocked("takeover_scope_unconfirmed"), None
        frozen = _source_snapshot(controller)
        target = _processing_request_target(controller, gate, path, ownership) if recovery else _request_target(controller, gate, path, ownership)
        if target is None:
            return _blocked("takeover_scope_unconfirmed"), None
        result = controller.prepare_target(target, RECHECK_BUDGET)
        if result["error"] is not None:
            return _blocked(result["error"]["reason"]), None
        blocked = controller.admission_rejection(owner_digest(ownership[0]))
        if blocked:
            return blocked, None
        if not _sources_unchanged(controller, frozen):
            return _blocked("takeover_scope_unconfirmed"), None
        authority = {"proof": result["proof"], "barrier": result["barrier"], "c_run_id": controller.c_run_id}
        return None, (authority, target, result, frozen)


# LLM: 默认0立即返回；先冻结当前锁代次。正式配置只为准确Count0正常停止捕获variant，fresh/MP原P1/P2及隔离保持，不先启动owner写者。
# 函数用途: 在持锁C启动中建立接手控制器并核实全部旧执行域，不创建Agent。
def prepare_coordinator_takeover(paths, count, c_run_id, *, config=None, config_path=None):
    if count == 0 or current_coordinator(paths.root, count) is None:
        return None
    lease = bind_coordinator_generation(paths.root, count, c_run_id)
    if lease.controller is not None:
        return lease.controller
    controller = CoordinatorTakeover(paths, count, c_run_id)
    lease.controller = controller
    if config is not None:
        from .standalone_migration import capture_standalone_migration

        capture_standalone_migration(controller, config, config_path)
    with controller._mutex:
        _refresh_targets(controller)
        for target in controller.targets:
            controller.prepare_target(target)
        controller._bootstrap_sources = _source_snapshot(controller)
    return controller


# LLM: 范围/当次时窗绑定原verify对象；lease仍活动，普通JSON或复制回执不能授权。
# 函数用途: 验本次直接调用的内存来源，不读证明文件。
def _current_receipt(controller, target, result, window) -> bool:
    state = new_quarantine_state(controller.c_run_id, target.scope, 0.0)
    state["next_check_at"] = window[0]
    _verdict, identifier = _fresh_verdict(state, result, window[1])
    return controller.active() and identifier is not None


# LLM: 旧未知范围不能遗忘；相同scope保持原state/退避，在途调用的全源末检仍拒绝同ID内容漂移。
# 函数用途: 为当前C隔离旧域建立或保留退避状态。
def _queue_target_recheck(controller, target) -> None:
    if target.ledger.root != controller.paths.root or target.ledger.partition.count != controller.count:
        controller._all_quarantined = True
        return
    if not controller.active() or not _scope_keys(target.scope):
        return
    key = _authority_bytes(target.scope)
    if key not in controller._rechecks:
        try:
            state = new_quarantine_state(controller.c_run_id, target.scope, time.monotonic())
            controller._rechecks[key] = (TakeoverTarget(target.ledger, copy.deepcopy(target.scope)), state)
        except ValueError:
            controller._all_quarantined = True
            return
    start_takeover_rechecks(controller)


# LLM: 与原撤权共门；只解除精确已停止P2范围，其它旧域及缺旧N限制仍并回，禁止局部清全域。
# 函数用途: 原子解除精确已停止范围并更新已核来源，不写准入占用。
def _mark_target_stopped(controller, target, bootstrap=None) -> None:
    with controller._lease._lifecycle_gate:
        if not controller.active() or controller._recheck_stop.is_set():
            raise ValueError("coordinator_lease_revoked")
        controller._rechecks.pop(_authority_bytes(target.scope), None)
        controller._quarantined.difference_update(_scope_keys(target.scope))
        remaining = set().union(*(_pending_restriction_keys(pending) for pending, _state in controller._rechecks.values()))
        controller._quarantined.update(remaining)
        if bootstrap is not None:
            controller._bootstrap_sources = bootstrap
        controller._condition.notify_all()


# LLM: 锁外完整目标调原P2、未知旧N只读原源；锁内冻结state身份/来源，迟到/替换无提交权或解除未知。
# 函数用途: 复核一个到期旧scope，保证观察不挡短等待，逐项通知。
def _recheck_target(controller, key, target, state):
    job = (key, target, state)
    with controller._mutex:
        frozen = _begin_recheck(controller, job)
    if frozen is None:
        return {"action": "discarded", "state": state}
    try:
        try:
            result = _observe_recheck(target)
        except (OSError, ValueError, TypeError, KeyError):
            with controller._mutex:
                controller._all_quarantined = True
            result = None
        with controller._mutex:
            return _finish_recheck(controller, job, (frozen, result))
    finally:
        with controller._condition:
            controller._inflight.discard(key)
            controller._condition.notify_all()


# LLM: 调用方持内存门；一次scope仅一原调用，前台更新或整域失效不能洗成旧任务的基线。
# 函数用途: 认领当前待复核项并冻结完整来源，不持锁执行内核观察。
def _begin_recheck(controller, job):
    key, target, state = job
    current = controller._rechecks.get(key)
    if (not controller.active() or controller._recheck_stop.is_set() or key in controller._inflight
            or current is None or current[1] is not state):
        return None
    if state["current_c_run_id"] != controller.c_run_id or not _refresh_targets(controller):
        decision = quarantine_recheck(state, None, time.monotonic())
        controller._rechecks[key] = (target, decision["state"])
        controller._condition.notify_all()
        return None
    controller._inflight.add(key)
    return _source_snapshot(controller)


# LLM: 原P2长预算只改观察期限；缺旧N精确variant只读原descriptor/字节，不进P2或产回执，漂移继续拒。
# 函数用途: 在门外复核原完整范围或原未知责任，永不从缓存签停止。
def _observe_recheck(target):
    if type(target) is UnresolvedExecutorSource:
        target.require_unchanged()
        return None
    try:
        return p2.verify_takeover(target.ledger, target.scope, budget=dict(RECHECK_BUDGET))
    except (OSError, ValueError, TypeError, KeyError):
        return None


# LLM: 原state身份仍相同且原C仍活动才消费；release_ready仅建议，完整核实/原子撤权门再决定解除。
# 函数用途: 锁内采用新回执或保守退避，丢弃撤权/替换后的迟到结果。
def _finish_recheck(controller, job, observed):
    key, target, state = job
    current = controller._rechecks.get(key)
    if (not controller.active() or controller._recheck_stop.is_set()
            or current is None or current[1] is not state):
        return {"action": "discarded", "state": state}
    decision = quarantine_recheck(state, observed[1], time.monotonic())
    try:
        if decision["action"] == "release_ready":
            _accept_recheck(controller, target, observed)
            return decision
    except (OSError, ValueError, TypeError, KeyError):
        decision = quarantine_recheck(state, None, time.monotonic())
    if controller.active() and not controller._recheck_stop.is_set():
        controller._rechecks[key] = (target, decision["state"])
    return decision


# LLM: 纯建议不是解除凭据；在同C内存门下再核当前lease、完整P1/P3源和内容寻址明细。
# 函数用途: 只解除精确已停止scope的隔离，不释放持久名额或签发P3。
def _accept_recheck(controller, target, observed) -> None:
    frozen, result = observed
    confirmed_evidence(target, result)
    if not controller.active() or not _sources_unchanged(controller, frozen):
        raise ValueError("takeover_evidence_source_changed")
    _mark_target_stopped(controller, target, frozen)


# LLM: 原绑定保留真实Popen/出生/P4与原租约；同原W的C内facade仅共用精确来源映射，新实例不能覆盖。
# 函数用途: 原W只登记一次来源对象，重复包装不丢既有来源，不签发owner许可。
def bind_current_worker_source(controller, issuer) -> None:
    from .multiprocess_worker_commit import WorkerCommitIssuer, WorkerCommitRejected

    if type(issuer) is not WorkerCommitIssuer or issuer._controller is not controller:
        raise WorkerCommitRejected("original_worker_source_required")
    issuer._require_current()
    issuer._require_worker(issuer.process.pid)
    with controller._mutex:
        index = issuer.launch.partition.index
        previous = controller._current_workers.get(index)
        if previous is not None:
            captured = controller._current_worker_origins.get(index)
            if (captured is None or captured[0] is not previous or previous.launch is not captured[1]
                    or previous.process is not captured[2] or captured[2] is not issuer.process
                    or captured[1] != issuer.launch):
                raise WorkerCommitRejected("original_worker_source_conflict")
            # 同一原句柄的包装不替换已绑定来源，也不从canonical补造历史；pin集合仍各自结清。
            issuer._executor_records = previous._executor_records
            return
        controller._current_workers[index] = issuer
        controller._current_worker_origins[index] = (issuer, issuer.launch, issuer.process)


# LLM: 仅首绑定的原C对象可退役空闲原W；要求真实Popen退出、零自产P1、原lease所有facade责任结清及完整源未变。
# 函数用途: 显式清掉空闲旧绑定，不启动新W、不清P1/隔离/启动基线；非空执行域必须另走完整接手。
def retire_idle_worker_source(controller, issuer):
    from .multiprocess_worker_commit import WorkerCommitIssuer, WorkerCommitRejected

    if (type(controller) is not CoordinatorTakeover or type(issuer) is not WorkerCommitIssuer
            or issuer._controller is not controller):
        raise WorkerCommitRejected('original_worker_source_required')
    # fork必须在任何继承的内存锁之前拒绝；原W已退出时不能调用只接受活W的登记核验。
    issuer._require_current()
    with controller._mutex:
        captured = next(((index, origin) for index, origin in controller._current_worker_origins.items()
                         if origin[0] is issuer), None)
        if captured is None:
            raise WorkerCommitRejected('original_worker_source_required')
        index, (_original, launch, process) = captured
        if (controller._current_workers.get(index) is not issuer
                or issuer.launch is not launch or issuer.process is not process):
            raise WorkerCommitRejected('original_worker_source_required')
        if process.poll() is None or issuer._executor_records:
            raise WorkerCommitRejected('original_idle_worker_required')
        prefix = (launch.partition.c_run_id, 'W', str(index), launch.instance_id)
        with controller._lease._lifecycle_gate:
            issuer._require_current()
            if any(pin.scope_key[:4] == prefix for pin in controller._lease._commit_pins):
                raise WorkerCommitRejected('original_worker_responsibility_pending')
        # 只读发现不占生命周期短门；canonical未知源不能被空map当空账洗掉。
        if not _sources_unchanged(controller, controller._bootstrap_sources):
            raise WorkerCommitRejected('original_worker_sources_changed')
        with controller._lease._lifecycle_gate:
            issuer._require_current()
            if (controller._current_workers.get(index) is not issuer
                    or controller._current_worker_origins.get(index) is not captured[1]
                    or issuer.launch is not launch or issuer.process is not process):
                raise WorkerCommitRejected('original_worker_source_required')
            if (process.poll() is None or issuer._executor_records
                    or any(pin.scope_key[:4] == prefix for pin in controller._lease._commit_pins)):
                raise WorkerCommitRejected('original_worker_responsibility_pending')
            del controller._current_workers[index]
            del controller._current_worker_origins[index]
        return True


# LLM: 全部P1先完整核读；仅原C认领或原W/P4实际提交的精确ID/摘要可排除，角色不由桶/JSON授信，同步C来源与W接手合同。
# 函数用途: 让合法当前C/W自产变化不污染旧域比较，启动已冻结ID、旧代、未知源仍走原P2隔离。
def _takeover_targets_only(controller, targets):
    from .multiprocess_worker_commit import WorkerCommitRejected

    baseline = controller._bootstrap_sources
    frozen_ids = {identifier for scope, _digests in json.loads(baseline[0])
                  for identifier in scope["executor_reservation_ids"]} if baseline is not None else set()
    result = []
    for target in targets:
        scope = target.scope
        if scope['role'] == 'C' and _current_coordinator_target(controller, target, frozen_ids):
            continue
        issuer = controller._current_workers.get(scope["worker_index"]) if scope["role"] == "W" else None
        exclude = (scope["c_run_id"] == controller.c_run_id and issuer is not None
                   and not frozen_ids.intersection(scope["executor_reservation_ids"])
                   and all(identifier in issuer._executor_records
                           and issuer._executor_records[identifier] == (target.digests or {}).get(identifier)
                           for identifier in scope["executor_reservation_ids"]))
        if exclude:
            try:
                issuer._require_current()
                issuer._require_worker(issuer.process.pid)
            except WorkerCommitRejected:
                exclude = False
        if not exclude:
            result.append(target)
    return result


# LLM: 本地来源须是原controller捕获的精确对象与原gate/claim/lease；逐ID对照实际自产摘要，不给同桶未知记录豁免。
# 函数用途: 从当前旧域比较中排除本C真正提交过的本地主体P1，其物理寿命责任仍留原lease账。
def _current_coordinator_target(controller, target, frozen_ids):
    from .multiprocess_coordinator_executor_source import CoordinatorExecutorSource
    from .multiprocess_worker_commit import WorkerCommitRejected

    scope = target.scope
    if scope['c_run_id'] != controller.c_run_id or frozen_ids.intersection(scope['executor_reservation_ids']):
        return False
    submitted = {}
    try:
        for source, origin in controller._current_coordinator_sources:
            if type(source) is not CoordinatorExecutorSource or source._sources is not origin or origin[4] is not controller:
                return False
            source._require_source_identity()
            for identifier, digest in source._executor_records.items():
                if identifier in submitted:
                    return False
                submitted[identifier] = digest
    except WorkerCommitRejected:
        return False
    return all(submitted.get(identifier) == (target.digests or {}).get(identifier)
               for identifier in scope['executor_reservation_ids'])


# LLM: closing仅原C物理域/host精确结清；内存门只冻结原refs，域观察及host join在门外；不发signal或按TTL/EOF清pin，同步C/native收尾测试。
# 函数用途: 等原内核域与末回送结清责任，未知域保pin且不持C短门等待消费者。
def collect_coordinator_executor_domains(controller):
    from .multiprocess_coordinator_executor_source import CoordinatorExecutorSource
    from .multiprocess_native_coordinator_host import CoordinatorNativeHostRoot
    from .multiprocess_worker_commit import WorkerCommitRejected

    with controller._mutex:
        sources = tuple(controller._current_coordinator_sources)
        roots = tuple(controller._current_coordinator_native_host_roots)
        for source, origin in sources:
            if type(source) is not CoordinatorExecutorSource or source._sources is not origin or origin[4] is not controller:
                raise WorkerCommitRejected('original_coordinator_executor_source_changed')
        for root in roots:
            if type(root) is not CoordinatorNativeHostRoot or not any(root.source is item[0] for item in sources):
                raise WorkerCommitRejected('original_coordinator_native_host_source_required')
            root._require_original()
    for source, _origin in sources:
        source.collect_completed_domains()
    for root in roots:
        root.collect_completed_hosts()


# LLM: P6只读首绑定的原C/W/source/host对象，退出后不可从P4/P1/清表补历史；锁只冻结引用，不等kernel/join，不授新的业务权。
# 函数用途: 给停止观察保留本代真实来源的完整原对象集合。
def original_drain_sources(controller):
    from .multiprocess_coordinator_executor_source import CoordinatorExecutorSource
    from .multiprocess_native_coordinator_host import CoordinatorNativeHostRoot
    from .multiprocess_worker_commit import WorkerCommitIssuer
    if type(controller) is not CoordinatorTakeover or not controller.active():
        raise ValueError('drain_original_coordinator_required')
    with controller._mutex:
        workers = tuple(controller._current_worker_origins.items())
        sources = tuple(controller._current_coordinator_sources)
        roots = tuple(controller._current_coordinator_native_host_roots)
        for index, (issuer, launch, process) in workers:
            if (type(issuer) is not WorkerCommitIssuer or controller._current_workers.get(index) is not issuer
                    or issuer.launch is not launch or issuer.process is not process or issuer._controller is not controller):
                raise ValueError('drain_original_worker_source_changed')
        for source, origin in sources:
            if type(source) is not CoordinatorExecutorSource or source._sources is not origin or origin[4] is not controller:
                raise ValueError('drain_original_coordinator_source_changed')
            source._require_source_identity()
        for root in roots:
            if type(root) is not CoordinatorNativeHostRoot or not any(root.source is source for source, _ in sources):
                raise ValueError('drain_original_host_source_changed')
            root._require_original()
    return workers, sources, roots


# LLM: 每次先核准确standalone来源或原角色关闭字节，再发现完整P1/旧N；丢源/漂移整体拒，不清未知或以历史替fresh P2。
# 函数用途: 更新原P1/P3及旧count未知责任，保持原末检和隔离，并在原异常被捕获后记录类型及代码位置。
def _refresh_targets(controller) -> bool:
    if not controller.active() or controller._all_quarantined:
        return False
    try:
        from .standalone_migration import current_standalone_migration

        migration = current_standalone_migration(controller)
        if migration is not None:
            migration.require_unchanged()
        controller._closed_role_sources = require_role_coverage(
            controller.paths, controller.c_run_id, count=controller.count, captured=controller._closed_role_sources,
            committed=_current_receipt_sources(controller),
            **({"standalone": migration} if migration is not None else {}))
        before = takeover_namespace_state(controller.paths)
        if controller._namespaces is not None and any(
            old is not None and (new is None or old[:2] != new[:2])
            for old, new in zip(controller._namespaces, before, strict=True)
        ):
            raise ValueError("takeover_namespace_lost")
        found = discover_takeover_targets(controller.paths, controller.count, current_c_run_id=controller.c_run_id)
        unresolved = tuple(value for value in found if type(value) is UnresolvedExecutorSource)
        _refresh_unresolved_sources(controller, unresolved)
        targets = _takeover_targets_only(controller, [value for value in found if type(value) is TakeoverTarget])
        owners = uncovered_admission_owners(controller.paths, targets, controller._accepted)
        if takeover_namespace_state(controller.paths) != before:
            raise ValueError("takeover_namespace_changed")
        controller._closed_role_sources = require_role_coverage(
            controller.paths, controller.c_run_id, count=controller.count, captured=controller._closed_role_sources,
            committed=_current_receipt_sources(controller),
            **({"standalone": migration} if migration is not None else {}))
        previous = [_authority_bytes(target.scope) for target in controller.targets]
        controller.targets, controller._unproven_owners, controller._namespaces = targets, owners, before
        for target in (value for value in targets if _authority_bytes(value.scope) not in previous):
            controller._quarantined.update(_scope_keys(target.scope))
            _queue_target_recheck(controller, target)
        return True
    except (OSError, ValueError, TypeError, KeyError) as error:
        controller._all_quarantined = True
        _log_source_refusal("source_refresh_failed", error)
        return False


# LLM: 只核原限制引用/字节，未知永不消失或换成新来源；使用唯一_rechecks调度，不造第二进度或P2 scope。
# 函数用途: 捕获旧count未知责任并持续阻原桶/当前owner桶，漂移整体拒绝。
def _refresh_unresolved_sources(controller, sources):
    current = {source.key(): source for source in sources}
    for original in controller._unresolved_sources:
        original.require_unchanged()
        replacement = current.get(original.key())
        if replacement is None or replacement.signature() != original.signature():
            raise ValueError('unresolved_executor_source_lost')
        current[original.key()] = original
    controller._unresolved_sources = tuple(current[key] for key in sorted(current))
    for source in controller._unresolved_sources:
        controller._quarantined.update(source.restriction_keys())
        key = source.key()
        if key not in controller._rechecks:
            controller._rechecks[key] = (source, unresolved_recheck_state(source, controller.c_run_id, time.monotonic()))
    if sources:
        start_takeover_rechecks(controller)


# LLM: 限制variant只有原拒绝键，完整target才消费原scope；未知不可能经局部P2成功清除。
# 函数用途: 汇总唯一调度中仍需保留的实际责任范围。
def _pending_restriction_keys(target):
    return target.restriction_keys() if type(target) is UnresolvedExecutorSource else _scope_keys(target.scope)


# LLM: 基线取当前调用开始时的不可变事实，原短路/查询次数不变；拒绝日志不区分未刷新与快照不同，不能充当来源或权限。
# 函数用途: 在启动副作用前及P3锁内对照当次P1/P3覆盖快照，原拒绝后只补静态位置诊断。
def _sources_unchanged(controller, frozen=None) -> bool:
    previous = _source_snapshot(controller) if frozen is None else frozen
    if not _refresh_targets(controller) or _source_snapshot(controller) != previous:
        controller._all_quarantined = True
        _log_source_refusal("source_guard_refused")
        return False
    return True


# LLM: 仅原拒绝后记录尾12函数:行与真实JSONDecodeError数值位置/正文字符长；不打印doc/msg/args/locals/filename，不改异常或权限，日志失败仍受原保护。
# 函数用途: 安全区分来源解析失败的产品调用链与位置，不保存正文或据日志授予恢复、重试和执行权。
def _log_source_refusal(stage, error=None):
    try:
        origins = []
        trace = error.__traceback__ if error is not None else None
        while trace is not None:
            origins.append(f"{trace.tb_frame.f_code.co_name}:{trace.tb_lineno}")
            trace = trace.tb_next
        error_number = getattr(error, "errno", None) if isinstance(error, OSError) else None
        json_position = (None, None, None)
        json_doc_chars = None
        if isinstance(error, json.JSONDecodeError):
            json_doc_chars = len(error.doc) if type(error.doc) is str else None
            if json_doc_chars is not None:
                json_position = tuple(value if type(value) is int and low <= value <= high else None
                                      for value, (low, high) in zip(
                                          (error.pos, error.lineno, error.colno),
                                          ((0, json_doc_chars), (1, json_doc_chars + 1), (1, json_doc_chars + 1)), strict=True))
        logging.getLogger(__name__).warning(
            "GATEWAY_TAKEOVER_SOURCE_REFUSED stage=%s error_type=%s errno=%s origin=%s "
            "json_pos=%s json_lineno=%s json_colno=%s json_doc_chars=%s",
            stage, type(error).__name__ if error is not None else "none",
            error_number if type(error_number) is int else None, "/".join(origins[-12:]) or "none",
            *json_position, json_doc_chars,
        )
    except Exception:
        pass


# LLM: 完整P1/P3、未知旧N、v2关闭及本次standalone固定签名入原末检；首publish后签名稳定，旧历史不授新执行权。
# 函数用途: 冻结原完整目标、缺源owner与限制来源，供同次短门末检。
def _source_snapshot(controller) -> tuple:
    from .standalone_migration import current_standalone_migration

    migration = current_standalone_migration(controller)
    result = (_target_signature(controller.targets), frozenset(controller._unproven_owners),
              tuple(source.signature() for source in controller._unresolved_sources), controller._closed_role_sources,
              _current_receipt_sources(controller))
    return result + (migration.signature(),) if migration is not None else result


# LLM: 含完整范围和每份canonical摘要，不能只比较owner/ID而忽略源内容漂移。
# 函数用途: 将本次发现事实转成内存比较字节，不写第二账本。
def _target_signature(targets) -> bytes:
    return _authority_bytes([(target.scope, target.digests) for target in targets])


# LLM: 直接重启也不能绕过分区总门；仅复用同次已核的完全相同scope，其它旧代/owner仍需当次P2a。
# 函数用途: 核实指定worker分区全部已发现旧域，返回首个隔离结果或None。
def _verify_worker_partition(controller, index, prepared=None):
    targets = [target for target in controller.targets
               if target.scope["role"] == "W" and target.scope["worker_index"] == index
               and target.scope != prepared]
    for target in targets:
        result = controller.prepare_target(target, RECHECK_BUDGET)
        if result["error"] is not None:
            return result
    return None


# LLM: 调用方仍持T/内存；只登记本次decision与规范读回完全相等的当前代占用，不读盘生成停止证明。
# 函数用途: 在自身P3决策区内记精确摘要，避免正常提交被后台误判；漂移整体隔离并拒绝返回成功。
def _remember_restamped(controller, path, decision) -> bool:
    from .multiprocess_records import record_digest
    from .multiprocess_takeover_sources import _read_admission_source

    if decision.get("status") != "admitted":
        return True
    try:
        row = _read_admission_source(controller.paths.root, ("admission", "reservations"), path.stem + ".json")
        digest = record_digest(row)
        if digest != record_digest(decision["reservation"]) or row["route"]["c_run_id"] != controller.c_run_id:
            raise ValueError("admission_commit_readback_changed")
        # 当前租约内的耐久提交已完成；此处仅记精确内容，撤权后的旧controller缓存不产生消费权。
        controller._accepted[row["request_id"]] = digest
        return True
    except (OSError, ValueError, TypeError, KeyError):
        controller._all_quarantined = True
        return False


# LLM: 仅产品工厂或组合根绑定；None也标为必须核实，不能退回仅检查JSON结构的纯记录合同。
# 函数用途: 将准入门绑定到当前C可信接手控制器；不写盘。
def bind_admission_takeover(gate, controller):
    gate._runtime_takeover_required = True
    gate._runtime_takeover = controller


# LLM: 冻结启动参数必须与真实C租约逐项相同，拒绝请求/文件冒充；只创建空闲W，不发信号或执行请求。
# 函数用途: 给启动组合根提供统一worker启动门，核实通过才调用原启动副作用。
def spawn_coordinator_worker(launch, spawn) -> dict:
    from .multiprocess_mode import _valid_launch

    lease = current_coordinator(launch.gateway_root, launch.partition.count)
    if (lease is None or lease.controller is None or lease.c_run_id != launch.partition.c_run_id
            or lease.pid != launch.parent_pid or not _valid_launch(launch)):
        return _blocked("coordinator_context_unavailable")
    return lease.controller.launch_worker(launch.partition.index, spawn)


# LLM: 旧域桶号和owner在当前count的重映射桶都保守隔离；只比旧桶会在N变化时先启动该owner的新分区。
# 函数用途: 只读核对指定worker是否关联任何未核实旧域，不把其它域局部停止当成解除该owner隔离。
def _partition_blocked(controller, index) -> bool:
    from ..user_space.owner_resolver import OwnerIdentity

    return (controller.admission_rejection(owner_digest(OwnerIdentity.local_main())) is not None
            or any(key[2] == index or int(key[-1], 16) % controller.count == index for key in controller._quarantined)
            or any(int(owner, 16) % controller.count == index for owner in controller._unproven_owners))


# LLM: 隔离键逐代逐角色逐分区逐owner，不合并为可被局部成功清除的一个全局owner布尔。
# 函数用途: 返回完整旧域的内存隔离键集合。
def _scope_keys(scope) -> set:
    return {(scope.get("c_run_id"), scope.get("role"), scope.get("worker_index"), owner)
            for owner in scope.get("owner_shas", [])}


# LLM: 只用于当前同步授权的字节绑定，不写文件；内容变化不能靠同对象身份绕过。
# 函数用途: 返回三字段授权的确定性字节。
def _authority_bytes(authority) -> bytes:
    return json.dumps(authority, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


# LLM: 请求/预留仅用于选原P1旧域；不会从请求构造执行者清单或默认空覆盖。
# 函数用途: 选择与请求旧代/owner/角色/分区一致的完整目标。
def _request_target(controller, gate, path, ownership):
    from .multiprocess_admission import _read_reservation
    from .multiprocess_route import owner_digest

    if gate.paths.root != controller.paths.root or gate.c_run_id != controller.c_run_id:
        return None
    record = _read_reservation(gate.paths.root / "admission" / "reservations" / (path.stem + ".json"))
    if not isinstance(record, dict) or not ownership:
        return None
    route = record.get("route") or {}
    owner_sha = owner_digest(ownership[0])
    role, index = ("C", None) if route.get("worker") == "C" else ("W", route.get("worker"))
    return next((target for target in controller.targets
                 if target.scope["c_run_id"] == route.get("c_run_id") and target.scope["role"] == role
                 and target.scope["worker_index"] == index and owner_sha in target.scope["owner_shas"]), None)


# LLM: 隔离决策只报告事实，不写等待正文或偷偷变更代次，保持唯一预留计数。
# 函数用途: 构造P2b统一拒绝，供启动/准入/改代按结构化字段消费。
def _blocked(reason) -> dict:
    return {"status": "blocked", "reason": reason, "error_code": "OWNER_PARTITION_QUARANTINED"}


# LLM: 此选择只服务原恢复metadata回调，先以nofollow单链接字节门拒绝硬链接/软链接别名，再从P1完整四绑定或运输来源定位旧域。
# 函数用途: 不让已补章P3覆盖原execution来源，每次重读同request和原P1来准备fresh P2。
def _processing_request_target(controller, gate, path, ownership):
    from ..common.nofollow_fs import read_bytes_beneath
    from .io import read_json_file_report
    from .multiprocess_processing_recovery import original_processing_target

    try:
        rel = path.relative_to(gate.paths.root).parts
        if read_bytes_beneath(gate.paths.root, rel, max_bytes=16 * 1024 * 1024, require_dir_fd=True) is None:
            return None
        report = read_json_file_report(path, context="gateway.processing_recovery.target")
        if report.load_error is not None or report.payload.get("id") != path.stem or not ownership:
            return None
        if report.payload.get("status") == "pending" and (
            (gate.paths.inbox / path.name).exists() or (gate.paths.terminal / path.name).exists()
        ):
            return None
        return original_processing_target(controller.targets, report.payload, owner_digest(ownership[0]))
    except (OSError, ValueError, TypeError, KeyError):
        return None


# LLM: 原baseline第五项只保固定writer已提交raw；不是第二registry/许可，不能从磁盘或任意callback补记。
# 函数用途: 取得当前C活期的原回执提交观察，默认尚无观察为空。
def _current_receipt_sources(controller):
    original = controller._bootstrap_sources
    return original[4] if original is not None and len(original) == 5 else ()


# LLM: 只在原固定receipt写叶同controller/lease/JSON窗口调用；旧raw须命中原观察，不能扫描目录收编或刷新P1/P3 baseline。
# 函数用途: 写前核唯一控制回执的原来源和完整原件，不授写权。
def require_current_receipt_write(source, path, payload, before):
    receipt = require_current_receipt_window(source, path, payload)
    controller = source.controller
    name = str(path.relative_to(controller.paths.root))
    if dict(_current_receipt_sources(controller)).get(name) != before:
        raise ValueError('control_original_commit_raw_changed')
    return receipt


# LLM: 固定source与规范路径在JSON锁创建前先核，Root/lease窗口缺失不得有锁或写副作用，锁内仍重复原校验。
# 函数用途: 验证这一次原回执末写窗口，不读文件或发行授权。
def require_current_receipt_window(source, path, payload):
    from .control_operation_service import (
        GatewayControlOperationReceipt,
        gateway_control_operation_path,
    )
    from .multiprocess_local_control import _require_receipt_origin

    controller, receipt = source.controller, GatewayControlOperationReceipt.from_dict(payload)
    _require_receipt_origin(source, receipt)
    if (not controller.active() or source.lease is not controller._lease
            or not controller._mutex._is_owned() or not controller._lease._lifecycle_gate._is_owned()
            or controller._recheck_stop.is_set()
            or path != gateway_control_operation_path(controller.paths, receipt.operation_id)):
        raise ValueError('control_original_commit_window_required')
    return receipt


# LLM: 原writer完整readback后才记这一项；只变第五项，原P1/P3/role比较值不刷新；末检失败锁存未知，不撤销已写事实。
# 函数用途: 在同一原写窗口保留本C回执raw，供旧closure识别真实增量。
def remember_current_receipt_write(source, path, payload, before, after):
    from ..common.json_io import serialize_private_json_no_newline
    from ..common.nofollow_fs import read_bytes_beneath

    require_current_receipt_write(source, path, payload, before)
    controller = source.controller
    name = str(path.relative_to(controller.paths.root))
    if (after != serialize_private_json_no_newline(payload, sort_keys=True).encode('utf-8')
            or read_bytes_beneath(controller.paths.root, tuple(name.split('/')),
                                  max_bytes=4 * 1024 * 1024, require_dir_fd=True) != after):
        controller._all_quarantined = True
        raise ValueError('control_original_commit_readback_changed')
    baseline = controller._bootstrap_sources
    if baseline is None:
        raise ValueError('control_original_commit_baseline_missing')
    commits = dict(_current_receipt_sources(controller))
    commits[name] = after
    controller._bootstrap_sources = (*baseline[:4], tuple(sorted(commits.items())))
    if not _sources_unchanged(controller, controller._bootstrap_sources):
        raise ValueError('control_original_commit_sources_changed')
