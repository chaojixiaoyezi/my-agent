# LLM: This C-local wrapper adds only lane lifetime checks around the original C/T/P1 gates; it is not a new writer or authority.
# 模块用途: 在真实host启动全程保留原attempt，物理spawn/R与本地退役同门，阻止process为空时错误排空。
import copy
import os

from .multiprocess_executor_start import _StartAttempt
from .multiprocess_worker_commit import WorkerCommitRejected


# LLM: Constructed only by the native host composition root; original source remains the sole admission/P1 authority.
# 类用途: 捕获原source和本次host绑定，仅在原短门中增加本地生命周期复核。
class NativeHostStartSource:
    # LLM: No IO or permissions are created here; retain exact host/source and original row binding, never reconstruct from a packet.
    # 函数用途: 固定真实host启动时的原来源和定位行。
    def __init__(self, host, row):
        self._origin, self._creator = self, os.getpid()
        self.host = self._host_origin = host
        self.original = host._source_origin
        self.root, self.partition = self.original.root, self.original.partition
        self._row = copy.deepcopy(row)

    # LLM: Copies/fork/rebinding reject before locks; active original C/T admission is still enforced by original.reserve/advance/perform_short_effect.
    # 函数用途: 本地退役/未启动状态不能进入真实创建门，来源字段不是新许可。
    def guard(self):
        host = self.host
        if (self._origin is not self or self._creator != os.getpid() or host is not self._host_origin
                or self.original is not host._source_origin or host._start_source is not self
                or self.root != self.original.root or self.partition != self.original.partition):
            raise WorkerCommitRejected('original_native_host_start_required')
        host._require_original()
        if host._retired or not host._starting:
            raise WorkerCommitRejected('original_native_host_lane_retired')

    # LLM: A row locator cannot change the exact original host request/attempt/tool/kind/reservation association.
    # 函数用途: 拒绝把本次host启动来源挪给另一个预留。
    def bound(self, row):
        keys = ('owner_sha', 'request_id', 'execution_attempt_id', 'lease_epoch', 'tool_call_id', 'kind', 'reservation_id')
        if self._row['kind'] != 'background_host' or any(row[key] != self._row[key] for key in keys):
            raise WorkerCommitRejected('original_native_host_binding_required')

    # LLM: Original C owns reserve; lane guard is rechecked in that same original gate and never substitutes permission.
    # 函数用途: 只通过原source预留本次host。
    def reserve(self, row):
        self.bound(row)
        return self.original.reserve(row, source_guard=self.guard)

    # LLM: Registration/abandonment keep the original authority and exact bound row; a closed request can still reject abandonment.
    # 函数用途: 沿原P1路径登记或保守尝试废弃，不另写账本。
    def advance(self, row, state, registration=None):
        self.bound(row)
        return self.original.advance(row, state, registration, source_guard=self.guard)

    # LLM: This is a read of the original ledger, not restoration of authority from a row.
    # 函数用途: 读回同一原canonical预留。
    def read(self, identifier):
        return self.original.read(identifier)

    # LLM: Failed attempt cleanup cannot bypass the original active gate.
    # 函数用途: 原样尝试废弃，失败由启动root保留unknown。
    def abandon(self, row):
        return self.advance(row, 'abandoned')

    # LLM: Lock order is the existing local lane -> original T/C; callback remains only physical spawn or nonblocking R, never readiness/frame/join.
    # 函数用途: 真实创建/放行与本地退役同门，最终仍走原C权限复核。
    def perform_short_effect(self, row, phase, callback):
        self.bound(row)
        self.guard()
        with self.host._lifecycle:
            self.guard()
            return self.original.perform_short_effect(row, phase, callback, source_guard=self.guard)


# LLM: Pass through the same original observer; this retains actual startup state before any physical effect, not a kernel tree witness.
# 类用途: C本地保存同一真实启动attempt，保持原三处观察点。
class NativeHostStartObserver:
    # LLM: Constructor has no side effects; the host/source were retained by the original composition root.
    # 函数用途: 捕获原lane及原观察者。
    def __init__(self, host, original):
        self.host, self.original = host, original

    # LLM: The actual start root calls this before reserve/spawn; failure later cannot drop the same WaitingExecutor/Popen.
    # 函数用途: 在真实创建前固定原尝试，关闭检查不再依赖尚为空的process字段。
    def after_attempt_created(self, attempt):
        host = self.host
        host._require_original()
        with host._lifecycle:
            if (type(attempt) is not _StartAttempt or attempt._origin is not attempt
                    or attempt.creator_pid != os.getpid() or attempt.ledger is not host._start_source
                    or host._retired or not host._starting or host._start_attempt is not None):
                raise WorkerCommitRejected('original_native_host_start_required')
            host._start_attempt = attempt
        original_hook = getattr(self.original, 'after_attempt_created', None)
        if original_hook is not None:
            original_hook(attempt)

    # LLM: The original observer sees the same durable reservation; it cannot replace admission.
    # 函数用途: 原样转发预留观察点。
    def after_reserved(self, row):
        self.original.after_reserved(row)

    # LLM: 同原StartAttempt实际Popen在R/任何poll前捕当前kernel birth并安装原waitstatus观察；合法末回送前禁止reap，不授权限，缺ABI保原协议/未知。
    # 函数用途: 保留实际host出生与退出事实，再转发原登记前观察。
    def after_spawned_before_registered(self, row, process):
        from .multiprocess_drain_process import OriginalWorkerExitObservation
        from .multiprocess_executor_start import _registration
        host = self.host
        host._require_original()
        if hasattr(os, 'waitid') and hasattr(os, 'WNOWAIT'):
            with host._lifecycle:
                host._require_original()
                if host._exit_observation is not None or host._start_attempt.waiting.process is not process:
                    raise WorkerCommitRejected('original_native_host_exit_observation_required')
                observation = OriginalWorkerExitObservation(process, _registration(process.pid)['process_identity'])
                host._exit_observation = host._exit_observation_origin = observation
                if observation.supported:
                    observation.install(reap_allowed=host._exit_reap_allowed)
        self.original.after_spawned_before_registered(row, process)

    # LLM: 最终原P1 readback的process_identity必须等R前捕获的实际birth，其他短门/观察原样；不从登记正文补出生历史。
    # 函数用途: 核本host原实际出生与准确P1一致，再转发R前观察。
    def after_registered_before_release(self, row, process):
        from .multiprocess_records import serialize_record
        host = self.host
        host._require_original()
        if host._exit_observation is not None and (host._exit_observation.process is not process
                or serialize_record(row['process_identity']) != host._exit_observation.identity_bytes):
            raise WorkerCommitRejected('original_native_host_exit_observation_required')
        self.original.after_registered_before_release(row, process)
