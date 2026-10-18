# LLM: Lane captures original C resources; unknown host startup/EOF/death never infers a responsibility finish.
# 模块用途: Own separate native host endpoints, authenticated connection and lifetime consumer with exact drain rules.
import copy
import os
import threading

from .multiprocess_kernel_message import (
    KernelMessageError,
    KernelMessageReceiver,
    KernelMessageSender,
)
from .multiprocess_native_control_receive import receive_control_packet_with_instance
from .multiprocess_native_host_authority import (
    begin_host_pin,
    coordinator_host_source,
    host_admission,
    host_lifecycle_gate,
)
from .multiprocess_native_host_session import NativeHostSession
from .multiprocess_native_host_wire import _CONNECTED, _READY, host_addresses
from .multiprocess_records import record_digest
from .multiprocess_worker_commit import WorkerCommitRejected


# LLM: Original C retains actual Popen/source/P1 early; unknown init/READY/send does not auto-finish or respawn.
# 类用途: Own one host-specific receiver/session/consumer and its lifetime responsibility pin.
class NativeHostLane:
    # LLM: 原spawn前捕本地门，P6出生观察稍后由同StartObserver绑定actualPopen；观察不授host/owner许可，默认资源/协议不变。
    # 函数用途: 建原host端点和启动状态，保本次实际退出观察槽。
    def __init__(self, source, row, observer):
        self._origin, self._creator = self, os.getpid()
        self.source, self.observer = source, observer
        self._source_origin = source
        self.addresses = host_addresses(source.root, source.partition, row['reservation_id'])
        self.receiver = KernelMessageReceiver(self.addresses[0])
        self._receiver_origin = self.receiver
        self._lifecycle = threading.RLock()
        self._lifecycle_origin = self._lifecycle
        self._retired = self._connecting = self._connected = False
        self._starting = False
        self._start_source = self._start_attempt = None
        self.process = self.row = self.session = self.thread = self.pin = None
        self._session_origin = None
        self.p3_digest = None
        self.sender_instance = self._sender_instance_origin = None
        self._stop = threading.Event()
        self.last_error = None
        self._exit_observation = self._exit_observation_origin = None

    # LLM: 原host/source/端点/门/AUDIT和R前exit观察必须同对象；观察只保存合法reap历史，不提前reap末回送，closing精确finish保持。
    # 函数用途: 拒绝host或其物理出生/退出观察换壳，不复活业务许可。
    def _require_original(self):
        if (self._origin is not self or self._creator != os.getpid() or self.source is not self._source_origin
                or self.receiver is not self._receiver_origin or self.session is not self._session_origin
                or self._lifecycle is not self._lifecycle_origin or self.sender_instance != self._sender_instance_origin):
            raise WorkerCommitRejected('original_native_host_lane_required')
        if self.process is not None and ((self.process,self.row) != self._process_origin or self.row != self._row_origin):
            raise WorkerCommitRejected('original_native_host_source_required')
        self.receiver._check_creator()
        self.source._require_source_identity()
        if self._exit_observation is not self._exit_observation_origin:
            raise WorkerCommitRejected('original_native_host_exit_observation_required')
        if self._exit_observation is not None:
            self._exit_observation.require_original()
            attempt = self._start_attempt
            if attempt is None or attempt.waiting is None or attempt.waiting.process is not self._exit_observation.process:
                raise WorkerCommitRejected('original_native_host_exit_observation_required')

    # LLM: 原host实际末回送前绝不reap；未R可信shim失败可真实回收，已R欠账不靠EOF/returncode解锁，不授新业务。
    # 函数用途: 给唯一内核退出观察判断原合法回收时点。
    def _exit_reap_allowed(self):
        self._require_original()
        attempt = self._start_attempt
        return (attempt is not None and not attempt.release_started
                or self.session is not None and self.session.completed is True)

    # LLM: Start intent is retained before lane publication; it is local lifecycle accounting, not original C/T permission.
    # 函数用途: process尚为空时也保留在途启动，关闭不能趁此退役。
    def begin_start(self, startup):
        from .multiprocess_native_host_start import NativeHostStartSource
        self._require_original()
        with self._lifecycle:
            self._require_original()
            if (self._retired or self._starting or self.process is not None or self._start_source is not None
                    or type(startup) is not NativeHostStartSource or startup.host is not self
                    or startup.original is not self._source_origin):
                raise WorkerCommitRejected('original_native_host_start_required')
            self._start_source = startup
            self._starting = True

    # LLM: Returning from the caller clears only the active intent, not the original attempt/Popen or uncertain physical responsibility.
    # 函数用途: 退出启动调用时保留未知失败事实，不把函数返回当未创建。
    def end_start(self):
        self._require_original()
        with self._lifecycle:
            self._starting = False

    # LLM: A failed/pre-remember attempt remains responsible after R or uncertain reap; a never-released actually reaped trusted shim may drain.
    # 函数用途: 用原attempt检查物理欠账，不能只看尚为空的process字段。
    def _start_pending(self):
        from .multiprocess_executor_start import _StartAttempt
        attempt = self._start_attempt
        if self._starting:
            return True
        if attempt is None:
            return False
        if (type(attempt) is not _StartAttempt or attempt._origin is not attempt
                or attempt.creator_pid != os.getpid() or attempt.ledger is not self._start_source):
            raise WorkerCommitRejected('original_native_host_start_required')
        if self.process is not None or attempt.waiting is None:
            return False
        return (attempt.release_started or attempt.cleanup is None
                or attempt.cleanup.get('process_reaped') is not True
                or attempt.waiting.process.returncode is None)

    # LLM: 真实R后先保存原Popen/P1；C主体pin已在首次reserve前，W保原issuer链；原同T/准入重新核、关闭不能迟到安装，核startup/R tests。
    # 函数用途: 捕获原host与原责任pin，不在端点已退役后补进新来源。
    def remember(self, launched):
        self._require_original()
        with self._lifecycle:
            self._require_original()
            if self._retired or self.process is not None:
                raise WorkerCommitRejected('original_native_host_lane_retired')
            self.process, self.row = launched.process, launched.reservation
            self._process_origin = (self.process,self.row)
            self._row_origin = copy.deepcopy(self.row)
            # LLM: Same original issuer/T/C and exact occupied P3; retain before anonymous handoff.
            # 函数用途: 保存已R的真实host责任，失败不自动丢P1或旧实例。
            def retain(pin):
                self.p3_digest = record_digest(host_admission(self.source, self.row))
                self.pin = pin
            if coordinator_host_source(self.source):
                if self.pin is None:
                    raise WorkerCommitRejected('original_native_host_start_pin_required')
                self.source._commit(lambda: host_admission(self.source, self.row))
            else:
                begin_host_pin(self.source, retain)

    # LLM: READY/CONNECTED等待门外，原C或W source同T/C短门核固定P3/epoch及原P1/host才启动Thread；角色不借issuer，失败保原pin，核C/W端点测试。
    # 函数用途: 一次连接在最终原请求门重核来源，启动异常保留原引用和未知责任。
    def connect(self):
        self._require_original()
        with self._lifecycle:
            self._require_original()
            if self._retired:
                raise WorkerCommitRejected('original_native_host_lane_retired')
            if self._connecting or self.session is not None or self.thread is not None:
                raise WorkerCommitRejected('original_native_host_already_connected')
            self._connecting = True
        sender = None
        try:
            payload, peer, instance = receive_control_packet_with_instance(self.receiver,timeout=5)
            if payload != _READY:
                raise WorkerCommitRejected('native_host_bootstrap_invalid')
            sender = KernelMessageSender(self.addresses[1])
            with self._lifecycle:
                self._require_original()
                if self._retired:
                    raise WorkerCommitRejected('original_native_host_lane_retired')
                self.session = self._session_origin = NativeHostSession(self,sender)
                self.session._require_host(peer)
                import sys
                if sys.platform == 'darwin':
                    if (type(instance) is not tuple or len(instance) != 3
                            or instance[:2] != ('darwin_audit_pidversion.v1', peer) or type(instance[2]) is not int):
                        raise WorkerCommitRejected('original_native_host_kernel_instance_required')
                    self.sender_instance = self._sender_instance_origin = instance
                # LLM: Original source._commit owns T then C; no new issuer/admission/owner is constructed in this callback.
                # 函数用途: 在真实Thread边界重核原host/P1及固定P3/epoch，再保留并启动同一消费者。
                def start_consumer():
                    from .io import read_json_file_report
                    self._require_original()
                    self.session._require_host(peer)
                    source = self._source_origin
                    p3 = host_admission(source, self.row)
                    payload = read_json_file_report(source._transition.request_path, context='native.host.consumer').payload
                    if record_digest(p3) != self.p3_digest or payload.get('lease_epoch') != self.row['lease_epoch']:
                        raise WorkerCommitRejected('original_native_host_binding_required')
                    thread = threading.Thread(target=self._consume,name='native-background-host',daemon=True)
                    self.thread = thread
                    thread.start()
                    self._connected = True
                self._source_origin._commit(start_consumer)
            # Blocking transport runs outside local/C/T lifecycle gates, after the actual consumer is published.
            sender.send(_CONNECTED,timeout=5)
        except BaseException:
            with self._lifecycle, host_lifecycle_gate(self._source_origin):
                self._retired = True
            if self.session is None and sender is not None:
                sender.close()
            raise
        finally:
            with self._lifecycle:
                self._connecting = False

    # LLM: Receiver stops only after C consumes the final exact host receipt; sending either C reply alone never finishes.
    # 函数用途: 保存实际接收错误，未知责任不靠超时/死亡推断结清。
    def _consume(self):
        while not self._stop.is_set():
            try:
                self._require_original()
                self.session.serve_once(timeout=5,receive_timeout=.1)
            except KernelMessageError as error:
                if error.error_code != 'KERNEL_MESSAGE_TIMEOUT':
                    self.last_error = error
                    self._stop.wait(.05)
            except Exception as error:
                self.last_error = error
                self._stop.wait(.05)
            finally:
                if self.session is not None and self.session.completed:
                    self._stop.set()
            self._stop.wait(.001)

    # LLM: 在途startup/实际attempt及C主体pre-spawn原pin即使process空仍挡排空；后续只消费原host精确末回送，不能静默关资源掩盖未知，核partial-init tests。
    # 函数用途: 未结清的在途创建、原host或命令责任都阻止端点排空。
    def require_drained(self):
        self._require_original()
        if (self._start_pending() or self.process is None and self.pin is not None
                or self.process is not None and (self.session is None or not self.session.completed or self.session._handles)):
            raise WorkerCommitRejected('original_native_host_responsibility_pending')

    # LLM: Drain check and irreversible retirement share remember's original local mutex; all actual joins remain outside T/C/local gates.
    # 函数用途: 排空与退役同门，不能在两者之间夹入已R来源；随后确认实际线程退出。
    def close(self):
        self._require_original()
        with self._lifecycle:
            self.require_drained()
            self._retired = True
            self._stop.set()
            thread = self.thread
        if thread is not None:
            if thread.ident is not None:
                thread.join(timeout=6)
            if thread.is_alive():
                raise WorkerCommitRejected('native_host_consumer_stop_unconfirmed')
        if self.session is None:
            self.receiver.close()
        else:
            self.session.close()


    # LLM: 仅精确C主体在物理创建前借原C source记pin，W早已有原start pin仍保原remember链；异常保持责任，callback仅受信C本地记账。
    # 函数用途: 让C后台host在预留和首次Popen以前便具有可追踪的原租约责任。
    def retain_start_lifetime(self, row):
        self._require_original()
        if not coordinator_host_source(self.source):
            return

        # LLM: 仅原begin_host_pin同步回调保存同一pin和规范P3摘要；不从消息或文件重新构造source/责任。
        # 函数用途: 为当前host保存真实创建责任及准入绑定，后续只能沿原收尾结清。
        def remember(pin):
            self.pin = pin
            self.p3_digest = record_digest(host_admission(self.source, row))
        begin_host_pin(self.source, remember, self._start_source.guard)
