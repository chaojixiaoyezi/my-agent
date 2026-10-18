# LLM: 仅原C的正count执行者实际spawn线程建立独立PID namespace；Popen直系init/P1/R不换链，无CAP_SYS_ADMIN失败关闭，无用户正文/argv判断；同步原launcher、P0/P1/P6和Linux真probe。
# 模块用途: 为Linux用户命令持有R前原namespace/init事实，借内核init退出屏障证明整个fork域结束。
import copy
import ctypes
import errno
import os
import subprocess
import sys
import threading
import time
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path

from .daemon_metadata import process_host_id
from .multiprocess_executor_identity import process_birth
from .multiprocess_worker_commit import WorkerCommitRejected

# Linux CLONE_NEWPID固定ABI位掩码，无量纲，不改变当前C或其它线程的namespace。
_CLONE_NEWPID_PROTOCOL = 0x20000000
# Linux CAP_SYS_ADMIN编号是固定能力协议值，无量纲；不通过sudo或放宽全局权限获得它。
_CAP_SYS_ADMIN_CODE = 21


# LLM: 内核域建立能力拒绝是结构化运行时失败，不允许改root权限或退回未contained Popen；原start仍保reserve/cleanup/禁止重放。
# 类用途: 让发行身份能力不足具有独立机器错误类型。
class NativeNamespaceUnavailable(WorkerCommitRejected):
    error_code = 'NATIVE_EXECUTION_DOMAIN_UNAVAILABLE'


# LLM: namespace dev/ino只作原内核引用定位，不能磁盘数值重建历史；PID namespace不能从原实例迁入祖先。
# 函数用途: 读取一个内核namespace引用的设备及inode。
def _namespace_identity(path):
    value = os.stat(path)
    return {'device': value.st_dev, 'inode': value.st_ino}


# LLM: 只为封闭birth区间的原线程for_children临时引用查询，不适用于保留的执行域引用或foreign清理。
# 函数用途: 核同线程恢复前后临时namespace坐标。
def _birth_descriptor_identity(descriptor):
    value = os.fstat(descriptor)
    return {'device': value.st_dev, 'inode': value.st_ino}


# LLM: 仅显式CAP检查，不给root名称自动授信；内核unshare/setns仍需实际成功，外部配置不修改。
# 函数用途: 在创建前拒绝没有PID namespace建立能力的发行身份。
def _require_capability():
    status = Path('/proc/thread-self/status').read_text()
    value = next((line.split(':', 1)[1].strip() for line in status.splitlines() if line.startswith('CapEff:')), None)
    if value is None or not int(value, 16) & (1 << _CAP_SYS_ADMIN_CODE):
        raise NativeNamespaceUnavailable('native_pid_namespace_capability_unavailable')


# LLM: 新native Linux预留显式v2；v1默认/设施和既存记录原样，不从wire字段或argv猜沙箱，不默补旧域证据。
# 函数用途: 给原用户执行命令绑定唯一C内namespace对象，原host/W设施不进入新域。
def prepare_native_domain(command, row, source, retained):
    if sys.platform != 'linux':
        return command, row
    source_row = {**row, 'schema_version': 'multiprocess_executor_reservation.v2', 'execution_domain': None}
    domain = NativePidNamespaceDomain(source, source_row)
    retained.append(domain)
    return replace(command, native_domain=domain), source_row


# LLM: 对象由原C组合根创建并随真实StartAttempt保留，不序列化、不按磁盘domain或新的namespace恢复它；原source/P1/出生身份不授新执行权。
# 类用途: 管理一次原Popen创建的独立Linux PID namespace及原init退出证明。
class NativePidNamespaceDomain:
    # LLM: 构造只保存原来源与v2预留，无系统namespace效果；真正权限/短门在spawn，失败对象也保留。
    # 函数用途: 冻结本命令原C来源，准备接收真实attempt及init。
    def __init__(self, source, row):
        from .multiprocess_executor_original_source import OriginalCExecutorSource
        from .multiprocess_native_host_source import _HostSource

        original = source.original if type(source) is _HostSource else source
        if type(original) is OriginalCExecutorSource:
            role = 'W'
        else:
            from .multiprocess_coordinator_executor_source import CoordinatorExecutorSource
            if type(original) is not CoordinatorExecutorSource or type(source) not in (_HostSource, CoordinatorExecutorSource):
                raise WorkerCommitRejected('original_native_namespace_source_required')
            role = 'C'
        if source.partition.count < 1:
            raise WorkerCommitRejected('original_native_namespace_source_required')
        self._origin = self
        self.creator_pid = os.getpid()
        self.source = self._source_origin = source
        self.original = source.original if type(source) is _HostSource else source
        self._original_source = self.original
        self.source_role = self._source_role_origin = role
        self.creator_identity = {'schema_version': 'multiprocess_process_identity.v1', 'host_id': process_host_id(),
                                 'pid': self.creator_pid,
                                 'start_time': process_birth(self.creator_pid)}
        from .multiprocess_records import validate_record
        validate_record(self.creator_identity)
        self._creator_identity_origin = copy.deepcopy(self.creator_identity)
        self.reserved = copy.deepcopy(row)
        self.attempt = self.process = self.row = self.fact = self._fact_origin = None
        self.namespace_fd = None
        self._reference_origin = None
        self._release_receipt = None
        self.observation_gap = False
        self.barrier_confirmed = False
        self.creation_attempted = False
        self.pin = None
        self._gate = threading.RLock()
        self._require_original()

    # LLM: 原C/source/attempt/Popen不可复制或换绑；原registered或同attempt未R失败的完整abandoned转换才读回匹配，namespace/退出前重复核，closing不恢复写权。
    # 函数用途: 拒绝外来或损坏的执行域对象，不消费其它命令的实例。
    def _require_original(self):
        if (self._origin is not self or self.creator_pid != os.getpid() or self.source is not self._source_origin
                or self.original is not self._original_source or self.source_role != self._source_role_origin
                or self.creator_identity != self._creator_identity_origin):
            raise WorkerCommitRejected('original_native_namespace_required')
        self.original._require_source_identity()
        if self.attempt is not None and (self.attempt._origin is not self.attempt
                or self.attempt.creator_pid != self.creator_pid or self.attempt.ledger is not self.source
                or self.attempt.domain is not self):
            raise WorkerCommitRejected('original_native_namespace_attempt_required')
        if self.process is not None and (self.process is not self._process_origin
                or getattr(self.process, '_native_execution_domain', None) is not self
                or self.process.pid != self._process_pid_origin
                or self.namespace_fd is not None and (self.process._internal_poll is not self._poll_origin
                    or self.process._wait is not self._wait_origin or self.process.send_signal is not self._signal_origin)):
            raise WorkerCommitRejected('original_native_namespace_process_required')
        if self.row is not None and (self.row != self._row_origin or not self._registration_current()):
            raise WorkerCommitRejected('original_native_namespace_registration_required')
        if self.namespace_fd is not None and self.namespace_fd is not self._reference_origin:
            raise WorkerCommitRejected('original_native_namespace_reference_required')
        if self.fact is not None and self.fact != self._fact_origin:
            raise WorkerCommitRejected('original_native_namespace_fact_required')

    # LLM: 原R前失败只能消费同attempt实际abandoned转换，完整process/domain/request字段仍等原registered行；不接受别记录/代次/磁盘重建或R后弱化。
    # 函数用途: 让正常未放行回收仍可沿原P1 FSM读回，原来源漂移继续拒绝。
    def _registration_current(self):
        actual = self.source.read(self.row['reservation_id'])
        if actual == self.row:
            return True
        return (self.attempt is not None and self.attempt.failed and not self.attempt.release_started
                and self.attempt.record == actual and actual == {**self.row, 'state': 'abandoned'})

    # LLM: W原issuer或精确C source封装的原lease是唯一责任账；start消息finish不等命令退出，域pin只在原T/C门创建，角色不由JSON授信。
    # 函数用途: 为普通native命令保留整个执行域寿命，host命令继续由原host lifetime pin覆盖。
    def retain_lifetime(self):
        self._require_original()
        if self.pin is not None:
            raise WorkerCommitRejected('original_native_namespace_pin_required')
        if self.source_role == 'C':
            self.pin = self.original.begin_execution_domain()
        else:
            # LLM: 只在原W source._commit门内向同一issuer登记域pin，不能凭角色文本或P1投影恢复原责任。
            # 函数用途: 在物理命令创建前保住原W执行域责任，不在这里启动或等待进程。
            def remember():
                self.pin = self.original.issuer.begin(self.original.owner, peer_pid=self.original._sources[6].pid)
            self.original._commit(remember)

    # LLM: 原对象/P1/内核退出屏障都匹配后才finish原域pin，closing可清原责任；未创建且未进入实际spawn只释放自己的原pin。
    # 函数用途: 结清原C物理域责任，控制消息结束不能提前结束它。
    def retire_lifetime(self):
        self._require_original()
        if self.pin is None:
            return True
        if self.process is None and not self.creation_attempted or self.empty(self.process, self.row):
            if self.source_role == 'C':
                self.original.finish_execution_domain(self.pin)
            else:
                self.original.issuer.finish(self.pin)
            self.pin = None
            return True
        return False

    # LLM: 只由原start的原attempt绑定，actual源/持久reserve必须仍相等，domain不能再次消费；不创造第二执行状态。
    # 函数用途: 在实际spawn之前保存原尝试，异常后不能丢已创建init。
    def bind_attempt(self, attempt):
        self._require_original()
        from .multiprocess_executor_start import _StartAttempt
        if (type(attempt) is not _StartAttempt or self.attempt is not None or attempt._origin is not attempt or attempt.creator_pid != self.creator_pid
                or attempt.ledger is not self.source or attempt.record != self.reserved or attempt.domain is not None):
            raise WorkerCommitRejected('original_native_namespace_attempt_required')
        self.attempt = attempt
        attempt.domain = self

    # LLM: 只修改当前spawn线程for_children并在同一finally恢复；实际Popen仍由唯一原设施创建，恢复失败sticky unknown、不尝试其它fork路径。
    # 函数用途: 让原Popen直接子进程出生为新域init，同时保证同C其它线程spawn不受影响。
    @contextmanager
    def birth(self):
        self._require_original()
        _require_capability()
        from .multiprocess_native_linux_reference import NativeNamespaceReference
        self._reference_origin = NativeNamespaceReference(self)
        lib = ctypes.CDLL(None, use_errno=True)
        lib.unshare.argtypes, lib.unshare.restype = [ctypes.c_int], ctypes.c_int
        lib.setns.argtypes, lib.setns.restype = [ctypes.c_int, ctypes.c_int], ctypes.c_int
        original_fd = os.open('/proc/thread-self/ns/pid_for_children', os.O_RDONLY | os.O_CLOEXEC)
        changed = False
        try:
            if _birth_descriptor_identity(original_fd) != _namespace_identity('/proc/self/ns/pid'):
                raise NativeNamespaceUnavailable('native_pid_namespace_parent_thread_drift')
            if lib.unshare(_CLONE_NEWPID_PROTOCOL) != 0:
                raise NativeNamespaceUnavailable('native_pid_namespace_unshare_denied')
            changed = True
            self.creation_attempted = True
            yield self
        finally:
            try:
                if changed and (lib.setns(original_fd, _CLONE_NEWPID_PROTOCOL) != 0
                        or _namespace_identity('/proc/thread-self/ns/pid_for_children') != _birth_descriptor_identity(original_fd)):
                    self.observation_gap = True
                    raise OSError(ctypes.get_errno() or errno.EIO, 'native_pid_namespace_restore')
            finally:
                os.close(original_fd)
                if self.process is None:
                    self._reference_origin.cancel_unborn()

    # LLM: 原Popen创建后立即由同C专用私有FD表捕获opaque引用并接真实pidfd waitid设施；登记前核host PID/birth/PPID及namespacePID1，仍阻塞R。
    # 函数用途: 保存真实独立init及namespace引用，不能靠正文声明或后置重建证明。
    def capture(self, process):
        self._require_original()
        if self.process is not None or type(process) is not subprocess.Popen or process.returncode is not None:
            raise WorkerCommitRejected('original_native_namespace_process_required')
        self.process = self._process_origin = process
        self._process_pid_origin = process.pid
        self._poll_origin = process._internal_poll
        self._wait_origin = process._wait
        self._original_poll = process._internal_poll
        self._original_wait = process._wait
        self._signal_origin = process.send_signal
        process._native_execution_domain = self
        process._native_command_domain = self
        self._reference_origin.capture(process)
        self.namespace_fd = self._reference_origin
        self._install_waiter(process)
        namespace = self._fd_identity(self.namespace_fd)
        parent_namespace = _namespace_identity('/proc/self/ns/pid')
        status = Path(f'/proc/{process.pid}/status').read_text()
        positions = next((line.split(':', 1)[1].split() for line in status.splitlines() if line.startswith('NSpid:')), [])
        parent = int(Path(f'/proc/{process.pid}/stat').read_text().rsplit(')', 1)[1].split()[1])
        if (not positions or int(positions[-1]) != 1 or int(positions[0]) != process.pid
                or parent != self.creator_pid or namespace == parent_namespace or process_birth(process.pid) is None):
            self.observation_gap = True
            raise WorkerCommitRejected('native_pid_namespace_pre_release_unconfirmed')
        self._namespace_origin = namespace
        self._parent_namespace_origin = parent_namespace

    # LLM: P1登记前核同一真实root/namespace引用及原R尚未开始；init identity直接使用原registration，不造第二birth规则。
    # 函数用途: 将唯一内核域事实随原P1 v2完整登记。
    def registration(self, registration):
        self._require_original()
        if (self.process is None or self.attempt.release_started or self.observation_gap
                or registration['process_identity']['pid'] != self.process.pid
                or self._fd_identity(self.namespace_fd) != self._namespace_origin
                or process_birth(self.process.pid) != registration['process_identity']['start_time']):
            raise WorkerCommitRejected('native_pid_namespace_pre_release_unconfirmed')
        fact = {'schema_version': 'multiprocess_execution_domain.v1', 'platform': 'linux', 'method': 'pid_namespace',
                'namespace': dict(self._namespace_origin), 'parent_namespace': dict(self._parent_namespace_origin),
                'init_identity': copy.deepcopy(registration['process_identity']), 'host_parent_pid': self.creator_pid,
                'init_namespace_pid': 1, 'creator_identity': copy.deepcopy(self.creator_identity),
                'source_role': self.source_role, 'source_worker_index': self.original.partition.index}
        self.fact = copy.deepcopy(fact)
        self._fact_origin = copy.deepcopy(fact)
        return fact

    # LLM: 原registered读回之后、R之前固定完整row；不能接受其它P1/新实例/磁盘重建kernel proof。
    # 函数用途: 绑定实际写入原规范账本的namespace事实。
    def registered(self, row):
        self._require_original()
        if (row.get('execution_domain') != self.fact or row['state'] != 'registered'
                or self.source.read(row['reservation_id']) != row or self.attempt.release_started):
            raise WorkerCommitRejected('original_native_namespace_registration_required')
        self.row = copy.deepcopy(row)
        self._row_origin = copy.deepcopy(row)

    # LLM: 原引用为专有C线程opaque对象，主C整数FD不再授信；fstat/close只在原私有表，丢引用不按数字重建。
    # 函数用途: 读取原kernel设施实际持有的namespace身份。
    def _fd_identity(self, descriptor):
        if descriptor is not self._reference_origin or descriptor is None:
            raise WorkerCommitRejected('original_namespace_reference_required')
        return descriptor.identity()

    # LLM: 原私有表设施真实pidfd waitid回执加R前不可逃namespace/init/原引用才能消费kernel屏障；可变returncode、根wait/空树均不授信。
    # 函数用途: 核准整个原PID域已由内核排空，原source或引用缺口永久unknown。
    def empty(self, process, row):
        self._require_original()
        with self._gate:
            if process is not self.process or row != self.row or self.observation_gap:
                return False
            if self.process is None:
                return not self.creation_attempted
            attempt = self.attempt
            if attempt is not None and attempt.failed and not attempt.release_started:
                if attempt.cleanup is not None and attempt.cleanup.get('process_reaped') is True:
                    return self._consume_original_barrier()
            return self._consume_original_barrier()

    # LLM: 屏障只消费原私有表设施真实pidfd waitid+释放回执，不读可变returncode或barrier_confirmed授信；丢设施/缓存污染永久unknown。
    # 函数用途: 验证原init已实际回收，正常释放原引用且不能关闭其它C线程的foreign FD。
    def _consume_original_barrier(self):
        if self.observation_gap:
            return False
        reference = self._reference_origin
        if reference is None or not hasattr(self, '_namespace_origin'):
            self.observation_gap = True
            return False
        if self._release_receipt is not None:
            return self._release_receipt is reference._receipt_origin
        self.process.poll()
        if self.observation_gap or self.process.returncode is None:
            return False
        try:
            receipt = reference.release()
        except WorkerCommitRejected:
            self.observation_gap = True
            return False
        if (receipt is None or receipt[0] is not reference or receipt[1] is not self.process
                or receipt[2] != self.process.pid or receipt[3] != (self.fact['init_identity']['start_time'] if self.fact else reference._birth)
                or dict(receipt[4]) != self._namespace_origin
                or os.waitstatus_to_exitcode(receipt[5]) != self.process.returncode):
            self.observation_gap = True
            return False
        self._release_receipt = receipt
        self.namespace_fd = None
        self.barrier_confirmed = True
        return True

    # LLM: 原Popen仍同对象，poll/wait仅委托原C设施的真实pidfd waitid；标准库缓存不再是回收权威，方法换绑在原origin门拒绝。
    # 函数用途: 保留原Popen API、stdout和signal语义，同时唯一消费内核回收事实。
    def _install_waiter(self, process):
        # LLM: Popen析构与poll调用同一内核设施，死态提示不制造回收；同步标准Popen签名。
        # 函数用途: 非阻塞查询原child，只将真实状态投影为returncode。
        def poll(_deadstate=None):
            return self._kernel_poll()
        # LLM: 超时与等待都在调用方门外，唯一设施每次只做非阻塞pidfd waitid；不能持原C生命周期门等待业务。
        # 函数用途: 保留原Popen wait的返回码和TimeoutExpired语义。
        def wait(timeout):
            return self._kernel_wait(timeout)
        # LLM: 信号只选择原pidfd，原C/source权限仍在调用门核；不向同PID新实例fallback。
        # 函数用途: 保留Popen terminate/kill语义而固定真实原实例。
        def send_signal(signum):
            self._kernel_signal(signum)
        process.send_signal = self._signal_origin = send_signal
        process._internal_poll = self._poll_origin = poll
        process._wait = self._wait_origin = wait

    # LLM: 原process防回收锁与设施串行，只有真实raw waitstatus更新缓存；活时非None缓存只标unknown且纠正显示，仍允许己方物理清理。
    # 函数用途: 非阻塞回收原init，外部污染returncode不能闭合域。
    def _kernel_poll(self):
        self._require_original()
        process = self.process
        if not process._waitpid_lock.acquire(False):
            return process.returncode
        try:
            return self.poll_locked()
        finally:
            process._waitpid_lock.release()

    # LLM: 仅原Popen锁内的原host/C观察接缝；实际状态由独占设施内核调用给出，缓存只作投影，不授权新操作。
    # 函数用途: 更新真实返回码，缺口保持unknown但不妨碍原实例清理。
    def poll_locked(self):
        self._require_original()
        reply = self._reference_origin.poll()
        status = reply.get('status')
        self.observation_gap = self.observation_gap or bool(reply.get('gap'))
        if status is None:
            if self.process.returncode is not None:
                self.observation_gap = True
                self.process.returncode = None
            return None
        actual = os.waitstatus_to_exitcode(status)
        if self.process.returncode is not None and self.process.returncode != actual:
            self.observation_gap = True
        self.process.returncode = actual
        return actual

    # LLM: 原Popen锁内真实WNOWAIT观察不reap，原source/调用门仍由host持有；可变缓存不能伪造退出。
    # 函数用途: 保留host peek/signal语义，尚活缓存污染成为永久观察缺口。
    def peek_locked(self):
        self._require_original()
        reply = self._reference_origin.peek()
        actual = reply.get('returncode')
        self.observation_gap = self.observation_gap or bool(reply.get('gap'))
        if self.process.returncode is not None and self.process.returncode != actual:
            self.observation_gap = True
            self.process.returncode = None
        return actual

    # LLM: 原source/实例检查先于防回收锁；真实pidfd只用于原实例清理，不恢复新写权限或按PID数字发信号。
    # 函数用途: 通过原设施给原init发送信号，已退出原实例不会转向复用PID。
    def _kernel_signal(self, signum):
        self._require_original()
        with self.process._waitpid_lock:
            self._reference_origin.send_signal(int(signum))

    # LLM: 真实Popen等待保持timeout语义，原C设施不阻塞pidfd waitid，不使用缓存假退出或另收别child。
    # 函数用途: 等本命令真实退出，超时不释放原namespace/pin。
    def _kernel_wait(self, timeout):
        deadline = None if timeout is None else time.monotonic() + timeout
        while True:
            result = self._kernel_poll()
            if result is not None:
                return result
            if deadline is not None and time.monotonic() >= deadline:
                raise subprocess.TimeoutExpired(self.process.args, timeout)
            time.sleep(.005)

    # LLM: 仅尝试早已创建且未R的真实init回收；异常引用保留unknown，不能在原C短门等待或自动重放用户命令。
    # 函数用途: 处理namespace创建后设施返回前失败留下的原Popen责任。
    def observe(self):
        self._require_original()
        attempt = self.attempt
        if (attempt is not None and attempt.failed and self.process is not None
                and attempt.waiting is None and not attempt.release_started):
            from .multiprocess_executor_process import WaitingExecutor, abort_waiting
            attempt.waiting = WaitingExecutor(self.process, -1, -1)
            attempt.cleanup = abort_waiting(attempt.waiting)
        if self.pin is not None:
            if self.process is not None:
                self.process.poll()
            self.retire_lifetime()
