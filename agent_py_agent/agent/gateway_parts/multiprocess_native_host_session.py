# LLM: 原host实际sender/Popen独立认证，原W issuer或精确C source及原P1均只保存在C；不继承W-only服务或由wire角色授信，同步host/C/P5测试。
# 模块用途: Consume native host commands, exact responsibility finishes and C-owned process observations.
import json
import os
import threading
import uuid
from dataclasses import asdict

from .multiprocess_executor_runtime import _reservation
from .multiprocess_executor_start import _registration, start_executor
from .multiprocess_kernel_message import KernelMessageSender
from .multiprocess_native_command_domain import NativeCommandDomainObserver
from .multiprocess_native_commit_session import _decode_packet
from .multiprocess_native_executor_session import (
    _executor_request_identity,
    _read_executor_command,
)
from .multiprocess_native_host_authority import (
    begin_host_pin,
    finish_host_pin,
    host_lifecycle_gate,
    host_pin_holder,
)
from .multiprocess_native_host_source import _HostSource
from .multiprocess_worker_commit import WorkerCommitRejected


# LLM: Host is a different actual instance, never presented to the original W session as W.
# 类用途: Consume host-only command/process/finish messages and preserve unknown responsibility.
class NativeHostSession:
    # LLM: 原C在host P1/R之后保存同原C源或W issuer及端点；独立host协议不构造假W，来源从不运输；同步kernel sender/closing tests。
    # 函数用途: Bind original host lane and separate responsibility table.
    def __init__(self, host, sender):
        host._require_original()
        host.receiver._check_creator()
        sender._check_creator()
        self._origin, self._creator_pid = self, os.getpid()
        self._sources = (host_pin_holder(host.source), host.receiver, sender)
        self._host_sources = self._sources
        self._lock = threading.RLock()
        self._last_seq = 0
        self._handles, self._processes, self._hosts = {}, {}, {}
        self._closed = False
        self._observer = host.observer
        self.host = host
        self._host_origin = host
        self.completed = False
        self._completion = None
        self._confirmation = None
        self._tree_receipts = {}
        # Keep pre-R kernel monitors for failed attempts too; a failed result does not prove no physical child.
        self._command_domains = []
        self._command_attempts = []
        self.command_source = _HostSource(self)

    # LLM: 原creator/source/端点在继承锁前核；精确C/W责任holder固定，closing只读身份仍允许末回送，新创建仍原T/P3/P5短门。
    # 函数用途: Guard creator, original host/source and independent endpoints before inherited locks.
    def _require_original(self):
        if self._origin is not self or self._creator_pid != os.getpid():
            raise WorkerCommitRejected('original_native_session_required')
        self._sources[1]._check_creator()
        self._sources[2]._check_creator()
        if self.host is not self._host_origin or self._sources != self._host_sources:
            raise WorkerCommitRejected('original_native_host_source_required')
        self.host._require_original()
        self.host.source._require_source_identity()

    # LLM: 同包peer核原Popen/P1；新命令必须原活birth/PGID/SID，末receipt若原WNOWAIT EXIT则Darwin仅原READY冻结AUDIT pidversion同包一致，Linux原未reap子实例；从不恢复执行权。
    # 函数用途: Authenticate real host without polling/reaping it or changing W identity.
    def _require_host(self, peer, *, terminal=False, sender_instance=None):
        self._require_original()
        process, source, row = self.host.process, self.host.source, self.host.row
        if (type(peer) is not int or peer != process.pid or process.returncode is not None
                or source.read(row['reservation_id']) != row):
            raise WorkerCommitRejected('original_native_host_process_required')
        if terminal:
            import sys
            if sys.platform == 'darwin' and (sender_instance is None or sender_instance != self.host.sender_instance):
                raise WorkerCommitRejected('original_native_host_kernel_instance_required')
            with process._waitpid_lock:
                exited = os.waitid(os.P_PID, peer, os.WEXITED | os.WNOHANG | os.WNOWAIT)
                if exited is not None and exited.si_pid == peer:
                    return
        if _registration(peer) != {k: row[k] for k in ('process_identity', 'pgid', 'sid')}:
            raise WorkerCommitRejected('original_native_host_process_required')

    # LLM: 同次kernel packet及不可变AUDITinstance配原host核，codec不授信；只有精确末回送可EXIT后认证，新start仍必须live原实例；回送失败保pin/P1，末receipt不发ACK，核真正短host竞态。
    # 函数用途: 消费一次原host认证消息，closing仍可精确收尾，不能重新委托或产生第二次末ACK。
    def serve_once(self, *, timeout=5, receive_timeout=None):
        self._require_original()
        with self._lock:
            if self._closed or self.completed:
                raise WorkerCommitRejected('native_session_closed')
            packet = self._sources[1].receive_fds(timeout=timeout if receive_timeout is None else receive_timeout)
            payload, peer, fds = packet
            try:
                request = _decode_packet(payload)
                self._require_host(peer, terminal=request.get('op') == 'host_done_received',
                                   sender_instance=getattr(packet, 'sender_instance', None))
                result = self._consume_host_request(request, fds, timeout)
                if result is not None:
                    KernelMessageSender.send(self._sources[2], json.dumps(result,separators=(',',':')).encode(),timeout=timeout)
                return result
            finally:
                for fd in fds:
                    os.close(fd)

    # LLM: serve已认证原host并持串行门；terminal received仍不产生新回送义务，所有seq/FD/源字段检查原样，未知责任不清。
    # 函数用途: 拆平原host消息分派，保持三段末回送与命令执行边界。
    def _consume_host_request(self, request, fds, timeout):
        if request.get('op') in {'host_done', 'host_done_confirm', 'host_done_received'}:
            result = self._complete_host(request, fds)
            return result
        if self._completion is not None:
            raise WorkerCommitRejected('original_native_host_responsibility_pending')
        if request.get('op') in {'executor_peek','executor_reap','executor_identity_v2','executor_tree_stop_v2'}:
            seq, op = request.get('seq'), request['op']
            if (set(request) != {'op','seq','process_token'} or type(seq) is not int or seq <= self._last_seq
                    or type(request['process_token']) is not str):
                raise WorkerCommitRejected('commit_request_invalid')
        else:
            seq, op = _executor_request_identity(request, self._last_seq)
        if op == 'finish':
            if fds or type(request['handle']) is not int or request['handle'] not in self._handles:
                raise WorkerCommitRejected('original_commit_handle_required')
            self._last_seq = seq
            issuer, pin = self._handles[request['handle']]
            finish_host_pin(issuer, pin)
            del self._handles[request['handle']]
            result = {'op':'finished','seq':seq,'handle':request['handle']}
        elif op in {'executor_peek','executor_reap','executor_signal','executor_identity_v2','executor_tree_stop_v2'}:
            if fds:
                raise WorkerCommitRejected('native_control_fds_unexpected')
            self._last_seq = seq
            result = self._process_command(request)
        else:
            row = self.host.row
            if (op != 'executor_start' or request['kind'] != 'background_command'
                    or any(request[k] != row[k] for k in ('request_id','execution_attempt_id','tool_call_id'))):
                raise WorkerCommitRejected('original_native_host_binding_required')
            command = _read_executor_command(request, fds, timeout)
            self._last_seq = seq
            result = self._start_command(request, command)
        return result

    # LLM: Both C replies require an authenticated exact consumption echo; only the last receipt releases the original pin.
    #   The terminal receipt has no reply/RPC of its own, so successful send is not projected as C completion.
    # 函数用途: 等真实host读到末回执并回送精确nonce，再在原C结清；末ACK丢失仍保留责任。
    def _complete_host(self, request, fds):
        seq, op = request.get('seq'), request['op']
        if (fds or type(seq) is not int or seq <= self._last_seq or self._handles
                or self._failed_attempt_pending()
                or any(not domain.empty(domain.process, domain.row) for domain in self._command_domains)
                or any(token not in self._tree_receipts or not self._tree_receipts[token].confirmed
                       for token in self._processes)):
            raise WorkerCommitRejected('original_native_host_responsibility_pending')
        if op == 'host_done':
            if set(request) != {'op', 'seq'} or self._completion is not None:
                raise WorkerCommitRejected('original_native_host_responsibility_pending')
            self._last_seq = seq
            self._completion = (seq, uuid.uuid4().hex)
            return {'op': 'host_done', 'seq': seq, 'nonce': self._completion[1]}
        if op == 'host_done_confirm':
            if (set(request) != {'op', 'seq', 'done_seq', 'nonce'}
                    or type(request.get('done_seq')) is not int or type(request.get('nonce')) is not str
                    or self._completion is None or self._confirmation is not None
                    or (request['done_seq'], request['nonce']) != self._completion):
                raise WorkerCommitRejected('original_native_host_completion_unconfirmed')
            self._last_seq = seq
            self._confirmation = (seq, uuid.uuid4().hex)
            return {'op': 'host_done_confirmed', 'seq': seq, 'nonce': self._confirmation[1]}
        if (op != 'host_done_received' or set(request) != {'op', 'seq', 'confirmed_seq', 'nonce'}
                or type(request.get('confirmed_seq')) is not int or type(request.get('nonce')) is not str
                or self._confirmation is None
                or (request['confirmed_seq'], request['nonce']) != self._confirmation):
            raise WorkerCommitRejected('original_native_host_completion_unconfirmed')
        self._last_seq = seq
        # The same actual host consumed the final exact C reply. This terminal receipt creates no new C reply obligation.
        finish_host_pin(host_pin_holder(self.host.source), self.host.pin)
        self.completed = True
        return None

    # LLM: Exact handle finish never removes an actual failed start; only a never-released shim with real confirmed reap may cease blocking.
    # 函数用途: 保留ready/monitor前未知回收及R后失败的物理责任，不把无domain当空域。
    def _failed_attempt_pending(self):
        from .multiprocess_executor_start import _StartAttempt
        for attempt in self._command_attempts:
            if (type(attempt) is not _StartAttempt or attempt._origin is not attempt
                    or attempt.creator_pid != os.getpid() or attempt.ledger is not self.command_source):
                raise WorkerCommitRejected('original_native_command_attempt_required')
            if attempt.failed and attempt.waiting is not None:
                if (attempt.release_started or attempt.cleanup is None
                        or attempt.cleanup.get('process_reaped') is not True
                        or attempt.waiting.process.returncode is None):
                    return True
        return False

    # LLM: 原C/W各留自己原lease pin再创建，Linux原线程域/P1 v2、Darwin原R前workloop；actual host与来源角色分开认证，失败保attempt/域；同步C/W正式host/P5/tree tests。
    # 函数用途: 固定原W命令责任并由原C创建，R前观察根历史，未知启动不丢域责任。
    def _start_command(self, request, command):
        source = self.command_source
        holder = host_pin_holder(source.original)
        # LLM: 原C或W source的pin只有责任作用，原当次host/source/T/P3守写权；不能把host sender当W。
        # 函数用途: 先保存本次命令的精确原pin，再创建实际执行者。
        def remember(pin):
            self._handles[request['seq']] = (holder, pin)
        begin_host_pin(source.original, remember, source.guard)
        row = _reservation(source, self.host.row, 'background_command', 'exec-' + uuid.uuid4().hex)
        from .multiprocess_native_linux_domain import prepare_native_domain
        command, row = prepare_native_domain(command, row, source, self._command_domains)
        observer = NativeCommandDomainObserver(self._observer, self._command_domains, source)
        try:
            launched = start_executor(source, row, command, observer)
        finally:
            # Failed starts may have physically created or released a child; never discard their original monitor.
            observer.observe_attempts()
            for domain in self._command_domains:
                domain.observe()
        if launched is None or not launched.ok:
            outcome = launched.error if launched is not None else {'ok':False,'error_code':'EXECUTOR_CONTEXT_REQUIRED'}
        else:
            token = launched.reservation['reservation_id']
            # Original source enables process cleanup after closing, without granting a new write.
            self._processes[token] = (launched.process, source.original, launched.reservation)
            outcome = {'ok':True,'process_token':token,'pid':launched.process.pid}
        return {'op':'applied','seq':request['seq'],'handle':request['seq'],'result':outcome}

    # LLM: C/W原source保同原Popen/P1及kernel peer；Linux用户reap仅原私有表设施，host设施WNOWAIT保末回送，C不借W issuer，同步background/P5。
    # 函数用途: Observe the original command, including short-lived Store birth and a v2 known-tree cleanup receipt.
    def _process_command(self, request):
        from .multiprocess_native_host_observation import (
            command_snapshot,
            stop_command_tree,
            store_birth,
        )
        entry = self._processes.get(request['process_token'])
        if entry is None:
            raise WorkerCommitRejected('original_executor_process_required')
        process,source,row = entry
        source._require_source_identity()
        if host_pin_holder(source) is not self._sources[0] or source.read(row['reservation_id']) != row:
            raise WorkerCommitRejected('original_executor_process_required')
        retained = getattr(process, '_native_host_retained', None)
        if retained is None:
            retained = {}
            process._native_host_retained = retained
        if request['op'] == 'executor_tree_stop_v2':
            # Known-instance cleanup can wait, but never holds original T/C gates or changes execution admission.
            receipt = stop_command_tree(process, row, retained)
            self._tree_receipts[request['process_token']] = receipt
            return {'op':'process','seq':request['seq'],'result':{'version':2,'receipt':asdict(receipt)}}
        if request['op'] == 'executor_identity_v2':
            snapshot, complete = command_snapshot(process, row, retained)
            retained.update(snapshot)
            process._native_host_observation_gap = getattr(process, '_native_host_observation_gap', False) or not complete
            return {'op':'process','seq':request['seq'],
                    'result':{'version':2,'birth':store_birth(row['process_identity']['start_time'])}}
        from .multiprocess_native_linux_domain import NativePidNamespaceDomain
        domain = getattr(process, '_native_execution_domain', None)
        if type(domain) is NativePidNamespaceDomain:
            with host_lifecycle_gate(source), process._waitpid_lock:
                result = domain.peek_locked()
                if request['op'] == 'executor_signal' and result is None:
                    if _registration(process.pid) != {k: row[k] for k in ('process_identity', 'pgid', 'sid')}:
                        raise WorkerCommitRejected('original_executor_process_required')
                    domain._reference_origin.send_signal(request['signal'])
                if request['op'] == 'executor_reap' and result is not None:
                    result = domain.poll_locked()
            return {'op': 'process', 'seq': request['seq'], 'result': {'returncode': result}}
        with host_lifecycle_gate(source), process._waitpid_lock:
            result = process.returncode
            if result is None:
                observed = os.waitid(os.P_PID,process.pid,os.WEXITED | os.WNOHANG | os.WNOWAIT)
                if observed is not None and observed.si_pid == process.pid:
                    result = observed.si_status if observed.si_code == os.CLD_EXITED else -observed.si_status
                if request['op'] == 'executor_peek':
                    snapshot, complete = command_snapshot(process, row, retained)
                    retained.update(snapshot)
                    process._native_host_observation_gap = getattr(process, '_native_host_observation_gap', False) or not complete
                if request['op'] == 'executor_signal' and result is None:
                    if _registration(process.pid) != {k:row[k] for k in ('process_identity','pgid','sid')}:
                        raise WorkerCommitRejected('original_executor_process_required')
                    os.kill(process.pid,request['signal'])
                if request['op'] == 'executor_reap' and result is not None:
                    pid,status = os.waitpid(process.pid,os.WNOHANG)
                    if pid != process.pid:
                        raise WorkerCommitRejected('original_executor_reap_unconfirmed')
                    process.returncode = os.waitstatus_to_exitcode(status)
                    result = process.returncode
        return {'op':'process','seq':request['seq'],'result':{'returncode':result}}


    # LLM: 只有全部原handles、失败attempt、命令内核域和host末回送已结清才关闭端点；不继承W-only commit服务，不自动丢责任。
    # 函数用途: 结束精确原host消费者，C/W角色各走自己的原source而非fakeissuer。
    def close(self):
        self._require_original()
        with self._lock:
            if self._handles or not self.completed:
                raise WorkerCommitRejected('original_native_host_responsibility_pending')
            if not self._closed:
                self._sources[1].close()
                self._sources[2].close()
                self._closed = True
