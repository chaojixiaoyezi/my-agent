# LLM: 原审批纯退休未结只同对象传播和核收；普通同码不升级，物理未知优先，不以清理完成签发终态或释放资源。
# LLM: W只消费C已准入pending；原池绑定出生观察，保actualThread和submit故障；坏时间须结构化拒绝，不从_threads清表猜退出，不重排/释放预留，同步原槽数和P5消费者测试。
# 模块用途: 把实际worker队列扫描、原claim和业务执行接在一起；异常只记claim摘要，仍保留未结执行。
import math
import time
from concurrent.futures import ThreadPoolExecutor

from .multiprocess_admission import _read_reservation, reservation_file
from .multiprocess_bootstrap import require_worker_bootstrap
from .multiprocess_control_worker import WorkerControlPump
from .multiprocess_dispatch_claim import ClaimCandidate, _read_locked_request, freeze_claim_snapshot
from .multiprocess_drain_resources import DrainResource
from .multiprocess_execution import execute_worker_claim
from .multiprocess_records import MultiprocessRecordError
from .paths import gateway_paths_from_root
from .queue_service import claim_request


# LLM: poll/stop/close由worker主循环串行调用；执行线程只运行原请求，不修改调度表或准入权威。
# 类用途: 用原有限槽位执行本分区任务；核收失败附安全摘要，心跳/未结Future与责任保持。
class WorkerRequestPump:
    # LLM: 先核P4冻结事实；原request池增加纯出生观察，不新增池/槽，actualworker进入业务前保存引用；控制独立槽/合法认领pool.get不变。
    # 函数用途: 保存本次worker的队列根、owner池和本地执行槽。
    def __init__(self, frozen, pool, capacity):
        require_worker_bootstrap(frozen, frozen.launch)
        if type(capacity) is not int or capacity < 1:
            raise ValueError("worker_capacity_invalid")
        self.frozen, self.pool, self.capacity = frozen, pool, capacity
        self.paths = gateway_paths_from_root(frozen.launch.gateway_root)
        self.claiming_stopped = False
        self._drain_resource = self._drain_origin = DrainResource('foreground', 'original-pool/0')
        self._executor = ThreadPoolExecutor(max_workers=capacity, thread_name_prefix="gateway-worker-request",
                                            initializer=self._drain_resource.remember_current_thread)
        self._drain_resource.bind_executor(self._executor)
        self._active = {}
        self.controls = WorkerControlPump(frozen, pool)

    # LLM: 包括前台及控制已返回但未核收future；不是六类排空证明。
    # 函数用途: 返回本调度器尚承担结算责任的前台和控制数。
    @property
    def inflight(self):
        return len(self._active) + self.controls.inflight

    # LLM: 控制独立扫描/原T锁claim和优先级不变；实际submit失败保同原出生缺口再传播，后台源不能凭文件名抢槽或重新认领。
    # 函数用途: 收取原执行结果，按原请求优先级填充本分区空闲槽位。
    def poll(self):
        self._reap()
        if self.claiming_stopped:
            return 0
        require_worker_bootstrap(self.frozen, self.frozen.launch)
        submitted = self.controls.poll()
        from .request_worker import _pending_request_sort_key

        for path in sorted(self.paths.inbox.glob("*.json"), key=_pending_request_sort_key):
            if len(self._active) >= self.capacity:
                break
            snapshot = self._candidate(path)
            if snapshot is None:
                continue
            try:
                claim = claim_request(self.paths, path, worker_snapshot=snapshot)
            except MultiprocessRecordError as error:
                self._report_rejection(path, error)
                continue
            if claim is None:
                continue
            try:
                future = self._executor.submit(execute_worker_claim, snapshot, claim, self.pool)
            except BaseException as error:
                self._drain_resource.retain_fault(error)
                raise
            self._active[future] = claim
            submitted += 1
        return submitted

    # LLM: 未盖章/外分区留原C；先核not_before_at含巨大整数的有限时间，再验证原预留/指纹/P5；坏字段只诊断，不提前执行或物化owner。
    # 函数用途: 从原文件构造已到期的认领候选，不加载用户资料或解析显示时间。
    def _candidate(self, path):
        try:
            request = _read_locked_request(path)
            route = request.get("route")
            if not isinstance(route, dict) or route.get("worker") != self.frozen.launch.partition.index:
                return None
            if request.get("status", "pending") != "pending" or request.get("cancel_requested") is True:
                return None
            not_before = request.get("not_before_at", 0)
            try:
                valid_time = (type(not_before) in (int, float)
                              and math.isfinite(not_before) and not_before >= 0)
            except OverflowError:
                valid_time = False
            if not valid_time:
                raise MultiprocessRecordError("request_not_before_invalid", "not_before_at")
            if not_before > time.time():
                return None
            reservation = _read_reservation(reservation_file(self.paths, path.stem))
            return freeze_claim_snapshot(self.frozen, ClaimCandidate(path, request, reservation))
        except (OSError, MultiprocessRecordError) as error:
            self._report_rejection(path, error)
            return None

    # LLM: 保留原请求供C按原规则处理；错误日志不含请求正文，也不代表自动恢复成功。
    # 函数用途: 记录本轮不能认领的明确原因。
    def _report_rejection(self, path, error):
        from .logging import _report_gateway_side_effect_error

        _report_gateway_side_effect_error("worker_claim_rejected", path.stem, error)

    # LLM: future异常向组合根传播；不删异常责任或把初始化失败写成业务成功，processing原件保留。
    # LLM: D32每轮只try原负向清理，忙继续controls；只有真实canonical终态/fullclaim与旧attempt已核收才删除，outer实际异常链仅保债。
    # 函数用途: 核收原Future；传播前仅输出本claim的安全hash诊断，不删失败责任或改变退休分支。
    def _reap(self):
        from ..conversation.agent_tool_approval import (
            complete_approval_retirement_future,
            find_approval_retirement_error,
        )

        for future, claim in tuple(self._active.items()):
            if not future.done():
                continue
            error = future.exception()
            if find_approval_retirement_error(error) is not None:
                if complete_approval_retirement_future(error, claim):
                    del self._active[future]
                continue
            try:
                if future.result() is not True:
                    raise RuntimeError("worker_request_not_processed")
            except BaseException as error:
                try:
                    from .logging import report_native_transport_diagnostic

                    report_native_transport_diagnostic('worker_future_result', error,
                        request_id=claim.request_id, execution_attempt_id=claim.execution_attempt_id)
                except BaseException:
                    pass
                raise
            del self._active[future]

    # LLM: 前台及控制单向停止认领，不中断当前执行；六类排空仍由上层P6负责。
    # 函数用途: 阻止下一轮接新请求。
    def stop_claiming(self):
        self.claiming_stopped = True
        self.controls.stop_claiming()

    # LLM: 原worker主循环串行消费，原池与首次group/source准确核后运输；actualThread出生历史不来自后读_threads，future/claim保原对象，不读写第二权威。
    # 函数用途: 给P6冻结准确前台及控制资源引用。
    def drain_snapshot(self):
        require_worker_bootstrap(self.frozen, self.frozen.launch)
        if self._drain_resource is not self._drain_origin or self._drain_resource.owner is not self._executor:
            raise ValueError('drain_original_worker_pool_changed')
        self._drain_resource._require_original()
        return self.claiming_stopped, self._drain_resource, tuple(self._active.items()), self.controls.drain_snapshot()

    # LLM: 任何stop/shutdown之前先核原pool/group refs，换源不能关闭foreign；原shutdown门外等待保出生历史，异常仍传播且另一原池关闭，不借退出释放P3。
    # LLM: 原退休Future或其异常链仍未核收必须抛原outer对象且保_active；清permission不代表物理排空，close不等文件锁。
    # 函数用途: 关闭原执行槽，保原未知请求与P3责任，不重排或释放。
    def close(self):
        self.drain_snapshot()
        self.stop_claiming()
        try:
            self._drain_resource.stop()
            self._executor.shutdown(wait=True)
            self._reap()
            from ..conversation.agent_tool_approval import find_approval_retirement_error

            for future in self._active:
                if future.done() and find_approval_retirement_error(future.exception()) is not None:
                    raise future.exception()
        finally:
            self.controls.close()
