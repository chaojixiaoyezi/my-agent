# LLM: 仅原C正count Linux用户域使用同进程私有fdtable设施；原init必须仍等R，不增加进程/权限/账；只有实际pidfd waitid结果可结屏障，联测domain/P5及foreign清理。
# 模块用途: 在原C隔离的描述符表中持有namespace引用，并保留原子进程真实回收事实。
import ctypes
import os
import queue
import signal
import threading
from pathlib import Path

from .multiprocess_executor_identity import process_birth
from .multiprocess_worker_commit import WorkerCommitRejected

# Linux close_range固定无量纲协议位；UNSHARE使本线程新表与其它C线程的关闭/复用无关。
_CLOSE_RANGE_UNSHARE_PROTOCOL = 2
# unsigned int完整FD上界为Linux ABI协议值，不是业务资源预算。
_LAST_DESCRIPTOR_PROTOCOL = 0xffffffff
# 原R前设施应答为同步局部内核操作；异常不回退到共享FD表。
_REPLY_TIMEOUT_SECONDS = 5.0
_KERNEL_FSTAT = os.fstat
_KERNEL_PIDFD_OPEN = getattr(os, 'pidfd_open', None)
_KERNEL_PIDFD_SIGNAL = getattr(signal, 'pidfd_send_signal', None)
_KERNEL_WAITID = os.waitid
_KERNEL_CLOSE = os.close


# LLM: 设施线程只能在原C实际source/P1待R尝试内出生；关闭C/错source/未reserve/复制拒绝，不把线程政策标签当许可。
# 函数用途: 在创建私表线程前核原实际执行尝试及原C当前门。
def _require_reference_creation(domain):
    from .multiprocess_native_host_authority import host_admission
    from .multiprocess_native_linux_domain import NativePidNamespaceDomain
    if type(domain) is not NativePidNamespaceDomain:
        raise WorkerCommitRejected('original_native_namespace_required')
    domain._require_original()
    attempt = domain.attempt
    if (attempt is None or attempt.failed or attempt.release_started or domain.process is not None
            or domain.creation_attempted or domain._reference_origin is not None):
        raise WorkerCommitRejected('original_native_namespace_attempt_required')
    original = domain.original
    if domain.source_role == 'W':
        original._require_original()
    elif not original._sources[4].active() or original._sources[5]._closing:
        raise WorkerCommitRejected('original_coordinator_executor_source_unavailable')
    if domain.source is not original:
        domain.source.guard()
    host_admission(original, domain.reserved)
    if domain.source.read(domain.reserved['reservation_id']) != domain.reserved:
        raise WorkerCommitRejected('original_native_namespace_registration_required')


# LLM: 原C封闭设施只接固定内存操作，不执行外来callback/argv，不向其它线程暴露可close整数；副本、fork、替换线程/引用不得消费原事实。
# 类用途: 让namespace引用的出生、核验、释放属于唯一私有表，同时保存同原init的实际waitstatus。
class NativeNamespaceReference:
    # LLM: 原reserve/attempt/source/P3/C门有效后，在pid_for_children改变前启动空私表线程；此时尚无init，capture仍原R前完成，失败不碰其他C表。
    # 函数用途: 建立原C封闭资源设施，等待唯一实际init捕获，不启动用户执行者。
    def __init__(self, domain):
        _require_reference_creation(domain)
        self._domain = self._domain_origin = domain
        self._origin = self
        self._creator = os.getpid()
        self._process = self._process_origin = None
        self._pid = self._birth = None
        self._requests, self._answers = queue.Queue(), queue.Queue()
        self._request_gate = threading.Lock()
        self._identity = self._identity_origin = None
        self._status = None
        self._gap = False
        self._closed = False
        self._receipt = self._receipt_origin = None
        self._thread = threading.Thread(target=self._run, name='native-namespace-reference', daemon=True)
        self._thread_origin = self._thread
        self._thread.start()
        reply = self._answer()
        if 'error' in reply:
            raise WorkerCommitRejected('native_namespace_reference_unavailable')


    # LLM: 只在空私表原设施中一次绑定原Popen；线程必须在pid_for_children改变前出生，实际namespace捕获仍原R前完成。
    # 函数用途: 将唯一实际init交给已出生的原C设施，不建立第二执行者。
    def capture(self, process):
        self._require()
        if self._process is not None or self._identity is not None:
            raise WorkerCommitRejected('original_namespace_reference_required')
        self._process = self._process_origin = process
        self._pid, self._birth = process.pid, process_birth(process.pid)
        reply = self._request(('capture', self._pid, self._birth))
        if reply.get('gap') or 'identity' not in reply:
            raise WorkerCommitRejected('native_namespace_reference_unavailable')
        self._identity = reply['identity']
        self._identity_origin = dict(self._identity)

    # LLM: 未捕获实际Popen的空设施可取消，只销毁自己的空表；已经捕获的资源必须另经原kernel回收或保持unknown。
    # 函数用途: 收回未启动引用设施，不触碰原C其他线程FD。
    def cancel_unborn(self):
        self._require()
        if self._process is not None:
            raise WorkerCommitRejected('original_namespace_reference_required')
        self._request('cancel')
        self._thread.join(_REPLY_TIMEOUT_SECONDS)

    # LLM: 这里只核原C/真实process/唯一线程和初始化原引用，不把角色标签或磁盘dev/ino当新许可。
    # 函数用途: 拒绝复制或被替换的原设施。
    def _require(self):
        if (self._origin is not self or self._creator != os.getpid() or self._process is not self._process_origin
                or self._thread is not self._thread_origin
                or self._process is not None and self._process.pid != self._pid
                or self._domain is not self._domain_origin
                or self._identity != self._identity_origin or self._receipt is not self._receipt_origin):
            raise WorkerCommitRejected('original_namespace_reference_required')

    # LLM: 应答来自唯一原设施，超时/死亡没有重新open或重放命令；所有query仅固定同步内核调用。
    # 函数用途: 读取封闭设施的原应答，缺口明确失败关闭。
    def _answer(self):
        try:
            return self._answers.get(timeout=_REPLY_TIMEOUT_SECONDS)
        except queue.Empty as error:
            self._gap = True
            raise WorkerCommitRejected('native_namespace_reference_observation_gap') from error

    # LLM: 不运输callback或新来源，固定op在同原设施串行；closed/gap不复活线程或namespace数字。
    # 函数用途: 查询原引用或原child实际waitstatus。
    def _request(self, operation):
        self._require()
        with self._request_gate:
            if self._closed or not self._thread.is_alive():
                status = self._receipt_origin[5] if self._receipt_origin is not None else self._status
                return {'gap': self._gap or not self._closed, 'closed': self._closed, 'status': status,
                        'receipt': self._receipt_origin, 'returncode': None if status is None else os.waitstatus_to_exitcode(status)}
            self._requests.put(operation)
            reply = self._answer()
            self._gap = self._gap or bool(reply.get('gap'))
            if reply.get('status') is not None:
                self._status = reply['status']
            self._closed = self._closed or bool(reply.get('closed'))
            if reply.get('receipt') is not None:
                self._receipt = self._receipt_origin = reply['receipt']
            return reply

    # LLM: 由实际私有表原FD的fstat返回，不在主C表借同数字查询；引用失去或设施死亡不返回数值重建证明。
    # 函数用途: 核对仍持有的原namespace引用。
    def identity(self):
        reply = self._request('identity')
        if reply.get('gap') or reply.get('closed') or reply.get('identity') != self._identity_origin:
            raise WorkerCommitRejected('native_namespace_reference_observation_gap')
        return dict(reply['identity'])

    # LLM: 唯一原pidfd waitid实际返回(pid,status)才能产生状态；Popen.returncode与正文/普通bool均不输入这个设施。
    # 函数用途: 非阻塞实际回收同一原init，返回真实raw waitstatus或仍活事实。
    def poll(self):
        reply = self._request('poll')
        return reply

    # LLM: 只观察同原child实际WNOWAIT，不消费或重建退出；poll原waitstatus存在时仍从该实际事实投影。
    # 函数用途: 保留原host peek不reap语义，signal时同一Popen锁防PID复用。
    def peek(self):
        return self._request('peek')

    # LLM: 仅原C Popen/source门后的信号，选择原pidfd而非可复用整数；实例已退出视为已终止，不signal另实例。
    # 函数用途: 发送信号到真实原init，PID复用不能转向外国进程。
    def send_signal(self, signum):
        reply = self._request(('signal', signum))
        if reply.get('signal_sent') is not True:
            raise WorkerCommitRejected('native_namespace_signal_observation_gap')

    # LLM: 原pidfd waitid状态已存在且原引用仍完整才close；同一私有表无其它C线程可在检查后复用FD，不称Linux条件close。
    # 函数用途: 消费原内核屏障并正常释放设施，回执以后线程退出销毁私有表。
    def release(self):
        reply = self._request('release')
        if reply.get('gap') or not reply.get('closed') or reply.get('status') is None:
            return None
        self._thread.join(_REPLY_TIMEOUT_SECONDS)
        receipt = reply.get('receipt')
        return receipt if receipt is self._receipt_origin and not self._thread.is_alive() else None

    # LLM: 明确丢失原引用只销毁其独占表并保持unknown，不关闭主C表任何数字；用于原设施故障/取消边界，不能授权结清。
    # 函数用途: 让丢失原引用成为永久缺口，仍可由原C管理真实init清理。
    def discard(self):
        self._request('discard')

    # LLM: close_range UNSHARE原子建立空私有表，失败不碰共享表；本线程不执行模型/插件/外来observer，初始FD从未在主C表出生。
    # 函数用途: 只清专用线程继承副本，保证后续引用只有原设施能关闭。
    def _private_table(self):
        lib = ctypes.CDLL(None, use_errno=True)
        call = lib.close_range
        call.argtypes, call.restype = [ctypes.c_uint, ctypes.c_uint, ctypes.c_int], ctypes.c_int
        if call(0, _LAST_DESCRIPTOR_PROTOCOL, _CLOSE_RANGE_UNSHARE_PROTOCOL) != 0:
            raise OSError(ctypes.get_errno(), 'native_namespace_private_table')

    # LLM: P_PIDFD选择R前真实原init句柄，外部提前reap/PID复用不变成另一child；无ABI或ECHILD只unknown，无numeric fallback。
    # 函数用途: 从原pidfd实际waitid消费退出事件，转换成标准Popen raw status。
    def _reap(self, pidfd, pid, status):
        if status is not None:
            return status
        observed = _KERNEL_WAITID(os.P_PIDFD, pidfd, os.WEXITED | os.WNOHANG)
        return None if observed is None else self._wait_status(observed, pid)

    # LLM: 原实际waitid结果只接受同原init退出/杀死/core三形态；保原exitcode/SIG/core语义，不接受正文或Popen缓存重建。
    # 函数用途: 将真实内核事件转换为POSIX waitstatus供原Popen投影。
    def _wait_status(self, observed, pid):
        if observed.si_pid != pid:
            raise WorkerCommitRejected('native_namespace_reap_instance_changed')
        if observed.si_code == os.CLD_EXITED:
            return observed.si_status << 8
        if observed.si_code in (os.CLD_KILLED, os.CLD_DUMPED):
            return observed.si_status | (0x80 if observed.si_code == os.CLD_DUMPED else 0)
        raise WorkerCommitRejected('native_namespace_reap_observation_gap')

    # LLM: 设施先出生为空私有表；原capture绑定实际init后才打开namespace，所有op固定，不执行外来callback/模型。
    # 函数用途: 持有及释放唯一原引用，真实pidfd waitid回执独立于Popen缓存；异常退出只销毁本私表并永久unknown。
    def _run(self):
        try:
            self._private_table()
            self._answers.put({'ready': True})
            operation = self._requests.get()
            if operation == 'cancel':
                self._answers.put({'closed': True})
                return
            if not isinstance(operation, tuple) or len(operation) != 3 or operation[0] != 'capture':
                raise WorkerCommitRejected('original_namespace_reference_required')
            _, pid, birth = operation
            if process_birth(pid) != birth:
                raise WorkerCommitRejected('native_namespace_reference_birth_changed')
            if _KERNEL_PIDFD_OPEN is None or _KERNEL_PIDFD_SIGNAL is None or not hasattr(os, 'P_PIDFD'):
                raise WorkerCommitRejected('native_namespace_pidfd_abi_unavailable')
            pidfd = _KERNEL_PIDFD_OPEN(pid, 0)
            fdinfo = Path(f'/proc/thread-self/fdinfo/{pidfd}').read_text()
            actual_pid = next((line.split(':', 1)[1].strip() for line in fdinfo.splitlines() if line.startswith('Pid:')), None)
            if actual_pid != str(pid):
                raise WorkerCommitRejected('native_namespace_pidfd_instance_unconfirmed')
            descriptor = os.open(f'/proc/{pid}/ns/pid', os.O_RDONLY | os.O_CLOEXEC)
            if process_birth(pid) != birth:
                raise WorkerCommitRejected('native_namespace_reference_birth_changed')
            value = _KERNEL_FSTAT(descriptor)
            identity = {'device': value.st_dev, 'inode': value.st_ino}
            self._answers.put({'identity': identity})
        except (OSError, AttributeError, WorkerCommitRejected):
            self._answers.put({'error': 'initialization', 'gap': True})
            return
        self._serve(descriptor, pidfd, pid, birth, identity)

    # LLM: 同原设施局部pid/birth，真实WNOWAIT不reap；既有raw status也仅由此前同child实际pidfd waitid产生。
    # 函数用途: 从内核观察退出码，避免peek分派深层嵌套或读取Popen缓存。
    def _peek(self, pidfd, pid, status):
        if status is not None:
            return os.waitstatus_to_exitcode(status)
        observed = _KERNEL_WAITID(os.P_PIDFD, pidfd, os.WEXITED | os.WNOHANG | os.WNOWAIT)
        if observed is None:
            return None
        return os.waitstatus_to_exitcode(self._wait_status(observed, pid))

    # LLM: 原pidfd固定真实instance，已退出ESRCH不改投别PID；其它内核失败仍gap，不能数字fallback。
    # 函数用途: 给原init发信号，正常退出竞态不污染责任。
    def _signal(self, pidfd, signum):
        try:
            _KERNEL_PIDFD_SIGNAL(pidfd, signum, None, 0)
        except ProcessLookupError:
            pass

    # LLM: 此循环仅原私表资源操作，实际pid/birth/identity冻结为局部，状态仅由原pidfd waitid产生；异常析构整个独占表不关其他C线程资源。
    # 函数用途: 查询和回收真实init；lost引用可继续物理清理，但不能发布原域释放证明。
    def _serve(self, descriptor, pidfd, pid, birth, identity):
        status, gap = None, False
        while True:
            operation = self._requests.get()
            try:
                if descriptor is not None:
                    value = _KERNEL_FSTAT(descriptor)
                    if {'device': value.st_dev, 'inode': value.st_ino} != identity:
                        raise WorkerCommitRejected('native_namespace_reference_observation_gap')
                code, signal_sent = None, False
                if operation in ('poll', 'release'):
                    status = self._reap(pidfd, pid, status)
                if operation == 'peek':
                    code = self._peek(pidfd, pid, status)
                if isinstance(operation, tuple) and len(operation) == 2 and operation[0] == 'signal':
                    self._signal(pidfd, operation[1])
                    signal_sent = True
                if operation == 'discard':
                    _KERNEL_CLOSE(descriptor)
                    descriptor, gap = None, True
                if operation == 'release' and status is not None and not gap:
                    _KERNEL_CLOSE(descriptor)
                    receipt = (self, self._process_origin, pid, birth, tuple(sorted(identity.items())), status)
                    self._answers.put({'closed': True, 'status': status, 'receipt': receipt})
                    return
                self._answers.put({'identity': identity, 'status': status, 'gap': gap, 'returncode': code, 'signal_sent': signal_sent})
                if gap and status is not None:
                    return
            except (OSError, WorkerCommitRejected):
                self._answers.put({'gap': True, 'status': status})
                return
