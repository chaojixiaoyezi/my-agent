# LLM: Original C installs this observer on a trusted waiting command shim before R. It proves ONLY a never-forked singleton's exit, not containment of an arbitrary tree.
# 模块用途: 由无FD workloop保存真实Darwin根FORK/EXIT和原队列关闭历史；fork、缺口或非原C使用均不结清。
import copy
import os
import select
import subprocess
import sys
import threading

from .multiprocess_executor_identity import process_birth
from .multiprocess_native_host_source import _HostSource
from .multiprocess_native_kernel_history import GuardedPipeDescriptor, GuardedQueue, KernelHistory
from .multiprocess_worker_commit import WorkerCommitRejected

# An unowned wrapper cannot safely close its old integer fd, including at Python GC; retain it until original C exits.
_UNOWNED_DOMAINS = []


# LLM: This object is C-local, never serialized or supplied by a host packet; a kernel registration precedes user-code release.
# 类用途: 固定等待垫片的实例和内核监控，只有完整未fork历史加EXIT才可能成为单实例证明。
class NativeSingletonDomain:
    # LLM: 原registered-before-R捕获两个guard原子出生队列及无FD原workloop；普通pipe只在封闭初始化局部持有，非原子guard出生，全部guard/历史安装后才发布；SPI或partial-init失败sticky unknown，核原故障/回收测试。
    # 函数用途: 在可信垫片仍等R时持有独立内核历史，两个数字FD全部复用仍不能丢FORK事实。
    def __init__(self, process, row, source):
        if type(source) is not _HostSource or source.session.command_source is not source:
            raise WorkerCommitRejected('original_native_host_source_required')
        source.guard()
        source.bound(row)
        from .multiprocess_executor_start import _StartAttempt
        attempts = source.session._command_attempts
        attempt = next((item for item in attempts if type(item) is _StartAttempt
                        and item._origin is item and item.creator_pid == os.getpid()
                        and item.ledger is source and item.waiting is not None
                        and item.waiting.process is process and item.registered_row == row), None)
        if (attempt is None or attempt.stage != 'readback' or attempt.release_started
                or attempt.failed or attempt.waiting.context_prepared or attempt.domain is not None
                or row.get('state') != 'registered' or source.read(row['reservation_id']) != row):
            raise WorkerCommitRejected('original_native_waiting_singleton_required')
        self._attempt = self._attempt_origin = attempt
        self._source = self._source_origin = source
        self._original_source = source.original
        self._origin = self
        self.creator_pid = os.getpid()
        self.process = process
        self.row = copy.deepcopy(row)
        self.birth = row['process_identity']['start_time']
        self.fork_observed = False
        self.exit_observed = False
        self.observation_gap = False
        self._queue = self._queue_origin = None
        self._witness = self._witness_origin = None
        self._history = self._history_origin = None
        self._kernel_gate = self._kernel_gate_origin = threading.RLock()
        self._marker_nonce = id(self)
        self._stamp = self._stamp_origin = None
        self._stamp_open = set()
        self._stamp_guards = {}
        self._queue_registered = False
        self._queue_ownership_lost = False
        pid = row['process_identity']['pid']
        if (type(process) is not subprocess.Popen or process.pid != pid or process.returncode is not None
                or row['pgid'] != pid or row['sid'] != pid or process_birth(pid) != self.birth
                or os.getpgid(pid) != pid or os.getsid(pid) != pid
                or os.waitid(os.P_PID, pid, os.WEXITED | os.WNOHANG | os.WNOWAIT) is not None):
            raise WorkerCommitRejected('original_native_waiting_singleton_required')
        attempt.domain = self
        try:
            self._queue = self._queue_origin = GuardedQueue()
            self._witness = self._witness_origin = GuardedQueue()
            reader, writer = os.pipe()
            # 普通pipe不能声称原子guard出生；此封闭初始化不发布FD或调用外部观察者。
            stamp_guards = {fd: GuardedPipeDescriptor(fd) for fd in (reader, writer)}
            os.write(writer, b'x')
            marker = select.kevent(reader, filter=select.KQ_FILTER_READ,
                                   flags=select.KQ_EV_ADD | select.KQ_EV_ENABLE, udata=id(self))
            change = select.kevent(pid, filter=select.KQ_FILTER_PROC,
                                   flags=select.KQ_EV_ADD | select.KQ_EV_ENABLE | select.KQ_EV_CLEAR,
                                   fflags=select.KQ_NOTE_FORK | select.KQ_NOTE_EXIT)
            self._queue.control([marker, change], 8, 0)
            self._witness.control([marker], 8, 0)
            self._history = self._history_origin = KernelHistory(pid, (self._queue.fileno(), self._witness.fileno()), (reader, writer))
            self._stamp = self._stamp_origin = (reader, writer, id(self))
            self._stamp_open.update((reader, writer))
            self._stamp_guards = stamp_guards
            self._queue_registered = True
            if any(os.get_inheritable(fd) for fd in (self._queue.fileno(), self._witness.fileno(), reader, writer)):
                self.observation_gap = True
        except BaseException as error:
            self.observation_gap = True
            self._close_queue()
            if not isinstance(error, Exception):
                raise
        if self.observation_gap:
            self._close_queue()

    # LLM: Require the original retained attempt, actual Popen, original aliases/gate and source; only its genuine not-released readback constructor may precede domain-table append. Copies/fork use fail before effects.
    # 函数用途: 核原尝试、R前原行、实际Popen与原C；只允许真实R前构造的短暂未入表阶段，不准迟到重建。
    def _require_original(self, process, row):
        attempt = self._attempt
        if (self._origin is not self or self.creator_pid != os.getpid() or process is not self.process
                or row != self.row or process.pid != self.row['process_identity']['pid']
                or self._queue is not self._queue_origin or self._stamp is not self._stamp_origin
                or self._witness is not self._witness_origin or self._kernel_gate is not self._kernel_gate_origin
                or self._history is not self._history_origin
                or self._source is not self._source_origin
                or self._source.original is not self._original_source
                or self._source.session.command_source is not self._source
                or attempt is not self._attempt_origin or attempt._origin is not attempt
                or attempt.creator_pid != os.getpid() or attempt.ledger is not self._source
                or attempt.domain is not self or attempt.waiting.process is not process
                or attempt.registered_row != self.row
                or not any(item is attempt for item in self._source.session._command_attempts)
                or (not any(item is self for item in self._source.session._command_domains)
                    and (attempt.stage != 'readback' or attempt.release_started or attempt.waiting.context_prepared))):
            raise WorkerCommitRejected('original_native_singleton_domain_required')
        self._source.session._require_original()
        self._original_source._require_source_identity()

    # LLM: 原FD关闭/复用后保留原workloop和责任直到C退出；guard包装无裸close析构器，unknown不能用于结清，核host排空调用方。
    # 函数用途: 永久保留未知责任和失去所有权的原对象，避免GC误关后来复用的数字FD。
    def _lose_queue_ownership(self):
        self.observation_gap = True
        if not self._queue_ownership_lost:
            self._queue_ownership_lost = True
            _UNOWNED_DOMAINS.append(self)

    # LLM: 原C锁内只读R前显式workloop的原disabled knote；不靠数字FD/marker/tag，不重CREATE/ADD；SPI读回失败sticky unknown，同步原双别名反例。
    # 函数用途: 读取不会随两个队列数字FD换代丢失的原FORK/EXIT和关闭日志。
    def _read_queue(self):
        self._require_original(self.process, self.row)
        with self._kernel_gate:
            self._require_original(self.process, self.row)
            if self._queue_ownership_lost:
                return False
            try:
                forked, exited = self._history.snapshot()
                self.fork_observed = self.fork_observed or forked
                self.exit_observed = self.exit_observed or exited
                return True
            except BaseException as error:
                self._lose_queue_ownership()
                if not isinstance(error, Exception):
                    raise
                return False

    # LLM: 原workloop disabled PROC保留ever-FORK位；任何原FD关闭日志作废观察，EXIT缓存只来自真实原快照；同步observe/empty/原host测试。
    # 函数用途: 非阻塞核真实队列代次及根历史；裸FD复用、关闭或陌生事件一律不能结清。
    def observe(self):
        self._require_original(self.process, self.row)
        queue = self._queue
        if self.observation_gap or self.exit_observed:
            return
        if queue is None or queue.closed:
            self._lose_queue_ownership()
            return
        self._read_queue()
        if self.observation_gap or self.exit_observed:
            self._close_queue()

    # LLM: Root wait/poll alone is never proof. Only the captured pre-R kernel history, actual EXIT and original C's real reaped child together establish a singleton-empty domain.
    # 函数用途: 未fork域有实测内核退出且原C已wait后才核准；fork域和缺口保持未知。
    def empty(self, process, row):
        self._require_original(process, row)
        self.observe()
        return (not self.observation_gap and not self.fork_observed and self.exit_observed
                and process.returncode is not None)

    # LLM: 正常退出先核原workloop，再用内核guard条件关闭；guard仅清理条件不是历史，partial-init和VANISHED全隔离，无Python裸close析构器；同步native回收测试。
    # 函数用途: 原队列完好时安全收资源，复用成陌生管道或队列时保留未知责任。
    def _close_queue(self):
        self._require_original(self.process, self.row)
        with self._kernel_gate:
            self._require_original(self.process, self.row)
            if self._queue_ownership_lost or self._queue is None or self._queue.closed:
                return
            if not self._queue_registered or not self._read_queue():
                self._lose_queue_ownership()
                return
            try:
                self._queue.close()
                self._witness.close()
                for descriptor in tuple(self._stamp_open):
                    self._stamp_guards[descriptor].close()
                    self._stamp_open.remove(descriptor)
                self._history.close()
            except OSError:
                self._lose_queue_ownership()
                return
            self._queue_registered = False


# LLM: This is a pass-through wrapper of the original start observer, not a launcher or alternate writer. Linux/unsupported platforms have no singleton proof in this implementation.
# 类用途: 只在native host命令的实际登记后、R前接入内核观察，保留原三个测试/生产观察点。
class NativeCommandDomainObserver:
    # LLM: No I/O at construction; attempts retained by original C include failures so an uncertain start cannot lose its monitor responsibility.
    # 函数用途: 保存原观察者和原C会话的尝试引用表。
    def __init__(self, original, retained, source):
        self.original = original
        self.retained = retained
        self.source = source
        self.created = []

    # LLM: Called by the actual start root before reserve/spawn; retain the exact attempt even if readiness or monitoring later fails.
    # 函数用途: 在物理创建之前保存原C尝试，失败结果不能丢WaitingExecutor/Popen。
    def after_attempt_created(self, attempt):
        self.source.session._require_original()
        if attempt.ledger is not self.source or attempt._origin is not attempt or attempt.creator_pid != os.getpid():
            raise WorkerCommitRejected('original_native_command_attempt_required')
        self.source.session._command_attempts.append(attempt)
        original_hook = getattr(self.original, 'after_attempt_created', None)
        if original_hook is not None:
            original_hook(attempt)

    # LLM: Forward the same real row without adding effects at E1.
    # 函数用途: 原样保留预留后的观察点。
    def after_reserved(self, row):
        self.original.after_reserved(row)

    # LLM: Forward the same real waiting process without claiming registration or release.
    # 函数用途: 原样保留登记前的等待实例观察点。
    def after_spawned_before_registered(self, row, process):
        self.original.after_spawned_before_registered(row, process)

    # LLM: The trusted shim has not run user code; install before the original callback and final canonical/R recheck. This grants no new execution authority.
    # 函数用途: 在业务代码运行前保存根FORK/EXIT历史，再执行原观察者。
    def after_registered_before_release(self, row, process):
        if sys.platform == 'darwin':
            domain = NativeSingletonDomain(process, row, self.source)
            process._native_command_domain = domain
            self.created.append(domain)
            self.retained.append(domain)
        self.original.after_registered_before_release(row, process)

    # LLM: Observe completed/aborted attempts after the original start pipeline returns; uncertain live ones remain retained, never quietly dropped.
    # 函数用途: 收集已发生的退出并清理相应FD，未知启动仍留在原会话中。
    def observe_attempts(self):
        for domain in self.created:
            domain.observe()
